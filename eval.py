from neo4j import GraphDatabase
import ollama
import time
import csv
import os
import sys
import re

# Force flush of print statements
sys.stdout.reconfigure(line_buffering=True)

# ---------- GLOBALS ----------
RAG_MODE = "machine"          # machine | ambiguity | knowledge | hybrid
PREFIX = RAG_MODE

# Fallback definitions (used only if Neo4j retrieval fails)
FALLBACK_DEFINITIONS = {
    "place": "A location name may refer to multiple places.",
    "relational": "Spatial relationship is vague or underspecified.",
    "geometric": "Spatial boundary or extent is unclear.",
    "granularity": "Required spatial scale is unclear.",
    "semantic": "Geographic concept has multiple interpretations.",
    "temporal": "Time period is ambiguous.",
    "part-whole": "Relationship between component and larger region is unclear.",
    "topological": "Uncertainty about spatial connectivity, adjacency, or intersection.",
    "attribute": "Properties or attributes of a geographic feature are ambiguous.",
    "intentional": "Query's underlying purpose or user's intended action is not explicit.",
    "multi-source": "Query implicitly combines information from multiple heterogeneous sources."
}
# -----------------------------

# ---------- Load SentenceTransformer ----------
print("⏳ Loading SentenceTransformer model...")
try:
    from sentence_transformers import SentenceTransformer, util
    embedder = SentenceTransformer('all-MiniLM-L6-v2')
    print("✅ SentenceTransformer model loaded successfully")
except ImportError as e:
    print(f"❌ Cannot load sentence-transformers: {e}")
    embedder = None

# ---------- Connect to Neo4j ----------
print("⏳ Connecting to Neo4j...")
try:
    driver = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=("neo4j", "Nadmin123")
    )
    with driver.session() as session:
        result = session.run("RETURN 1")
        result.single()
    print("✅ Neo4j connection established successfully")
except Exception as e:
    print(f"❌ Neo4j connection error: {e}")
    driver = None

# ---------- Helper functions ----------
def extract_keywords(query):
    """Extract meaningful keywords from a query."""
    words = re.findall(r'\b[a-zA-Z]{3,}\b', query.lower())
    stopwords = {
        "what", "where", "which", "when", "who", "the", "and", "for",
        "from", "with", "near", "between", "city", "town", "is", "are",
        "of", "in", "on", "at", "to", "by", "how", "why"
    }
    return [w for w in words if w not in stopwords]

def normalize_ambiguity(text):
    """Normalize ambiguity string: lowercase, replace dashes/spaces with '-'."""
    text = text.lower()
    text = text.replace("–", "-").replace("—", "-").replace("_", "-")
    text = text.replace(" ", "-")
    return text

def ask_mistral(prompt):
    """Send prompt to Mistral 7B and return response."""
    response = ollama.chat(
        model="mistral:7b",
        messages=[{"role": "user", "content": prompt}]
    )
    return response["message"]["content"]

# ---------- Ambiguity-RAG (definitions from Neo4j) ----------
def get_ambiguity_definitions_from_neo4j(ambiguity_text):
    """Retrieve definitions for ambiguity types that match the normalized text."""
    normalized = normalize_ambiguity(ambiguity_text)
    cypher = """
    MATCH (a:AmbiguityType)-[:HAS_DEFINITION]->(d:Definition)
    RETURN a.name AS name, d.text AS definition
    """
    definitions = []
    try:
        with driver.session() as session:
            results = session.run(cypher)
            for record in results:
                type_key = record["name"].lower().replace(" ", "-")
                if type_key in normalized:
                    definitions.append(record["definition"])
    except Exception as e:
        print(f"⚠️ Error retrieving definitions: {e}")
    return definitions

def ambiguity_rag(query, ambiguity):
    """Ambiguity-RAG: retrieve definitions and include in prompt."""
    definitions = get_ambiguity_definitions_from_neo4j(ambiguity)
    if not definitions:
        # Fallback: hardcoded keyword match
        normalized = normalize_ambiguity(ambiguity)
        for key, definition in FALLBACK_DEFINITIONS.items():
            if key in normalized:
                definitions.append(definition)
    ambiguity_context = "\n".join(definitions)
    prompt = f"""
You are a geospatial ambiguity expert.

Ambiguity information:
{ambiguity_context}

Query:
{query}

Provide only the intended interpretation.
Do not explain your reasoning.
Output one concise interpretation.
"""
    return ask_mistral(prompt)

# ---------- Knowledge-RAG (lexical retrieval with safe type handling) ----------
def retrieve_spatial_facts(query):
    """
    Lexical keyword retrieval with Python-side filtering to handle array properties safely.
    """
    keywords = extract_keywords(query)
    if not keywords:
        return "", 0
    
    # Fetch nodes (limit to 100 to keep it manageable)
    cypher = """
    MATCH (n)
    WHERE NOT (n:GeoQuery OR n:AmbiguityType OR n:IntendedInterpretation OR n:Definition)
    RETURN n
    LIMIT 100
    """
    facts = []
    try:
        with driver.session() as session:
            results = session.run(cypher)
            for record in results:
                node = record["n"]
                matched = False
                
                # Check all properties for keyword matches
                for k, v in node.items():
                    if v is None:
                        continue
                    
                    # Handle different data types
                    if isinstance(v, list):
                        # List of values (e.g., StringArray)
                        for item in v:
                            if item is not None:
                                item_str = str(item).lower()
                                if any(kw in item_str for kw in keywords):
                                    matched = True
                                    break
                    elif isinstance(v, (str, int, float, bool)):
                        # Scalar value
                        val_str = str(v).lower()
                        if any(kw in val_str for kw in keywords):
                            matched = True
                    # Skip complex objects (dicts, spatial types, etc.)
                    
                    if matched:
                        break
                
                if matched:
                    # Format node for output
                    facts.append(f"{list(node.labels)}: {dict(node.items())}")
                
                if len(facts) >= 20:
                    break
    except Exception as e:
        print(f"Error retrieving facts: {e}")
    
    return "\n".join(facts), len(facts)

def knowledge_rag(query):
    """Knowledge-RAG: inject retrieved spatial facts."""
    facts_str, count = retrieve_spatial_facts(query)
    if count == 0:
        facts_str = "No specific spatial facts found."
    prompt = f"""
You are a geospatial reasoning expert.

Spatial knowledge:
{facts_str}

Question:
{query}

Provide only the intended interpretation.
Do not explain your reasoning.
Output one concise interpretation.
"""
    return ask_mistral(prompt), facts_str, count

# ---------- Hybrid-RAG ----------
def hybrid_rag(query, ambiguity):
    """Hybrid-RAG: combine definitions and spatial facts."""
    definitions = get_ambiguity_definitions_from_neo4j(ambiguity)
    if not definitions:
        normalized = normalize_ambiguity(ambiguity)
        for key, definition in FALLBACK_DEFINITIONS.items():
            if key in normalized:
                definitions.append(definition)
    ambiguity_context = "\n".join(definitions)
    facts_str, count = retrieve_spatial_facts(query)
    if count == 0:
        facts_str = "No specific spatial facts found."
    prompt = f"""
You are a geospatial ambiguity expert.

Ambiguity information:
{ambiguity_context}

Spatial knowledge:
{facts_str}

Question:
{query}

Provide only the intended interpretation.
Do not explain your reasoning.
Output one concise interpretation.
"""
    return ask_mistral(prompt), facts_str, count

# ---------- Model Router ----------
def run_model(query, ambiguity):
    """Route to appropriate RAG mode."""
    if RAG_MODE == "machine":
        return machine_only(query), "", 0
    elif RAG_MODE == "ambiguity":
        pred = ambiguity_rag(query, ambiguity)
        return pred, "", 0
    elif RAG_MODE == "knowledge":
        pred, context, count = knowledge_rag(query)
        return pred, context, count
    elif RAG_MODE == "hybrid":
        pred, context, count = hybrid_rag(query, ambiguity)
        return pred, context, count
    else:
        raise ValueError(f"Unknown RAG_MODE: {RAG_MODE}")

# ---------- Data loaders ----------
def load_queries():
    """Load queries from Neo4j (local)."""
    print("⏳ Loading queries from Neo4j...")
    cypher = """
    MATCH (q:GeoQuery)-[:HAS_AMBIGUITY]->(a:AmbiguityType)
    MATCH (q)-[:INTENDED_MEANING]->(i:IntendedInterpretation)
    RETURN q.text AS query, a.name AS ambiguity, i.meaning AS meaning
    """
    data = []
    try:
        with driver.session() as session:
            results = session.run(cypher)
            for r in results:
                data.append({
                    "query": r["query"],
                    "ambiguity": r["ambiguity"],
                    "meaning": r["meaning"] or ""
                })
        print(f"✅ Loaded {len(data)} queries from Neo4j")
    except Exception as e:
        print(f"❌ Neo4j error: {e}")
    return data

def load_control_queries(csv_path="1089Control.csv"):
    print(f"⏳ Loading control queries from {csv_path}...")
    data = []
    if not os.path.exists(csv_path):
        print(f"❌ Control CSV not found: {csv_path}")
        return data
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 3:
                    query, ambiguity, meaning = row[0].strip(), row[1].strip(), row[2].strip()
                    data.append({
                        "query": query,
                        "ambiguity": ambiguity or "Control",
                        "meaning": meaning or "Expected correct answer"
                    })
    except Exception as e:
        print(f"❌ Error reading control CSV: {e}")
    print(f"✅ Loaded {len(data)} control queries")
    return data

def load_main_queries(csv_path="200queries.csv"):
    print(f"⏳ Loading main queries from {csv_path}...")
    data = []
    if not os.path.exists(csv_path):
        print(f"❌ Main CSV not found: {csv_path}")
        return data
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                query = row.get('Query', '').strip()
                primary = row.get('Primary Category', '').strip()
                secondary = row.get('Secondary Categories', '').strip()
                gt = row.get('Ground Truth', '').strip()
                if not query:
                    continue
                ambiguity = primary
                if secondary and secondary != '—':
                    ambiguity += f" | {secondary}"
                data.append({
                    "query": query,
                    "ambiguity": ambiguity or "Uncategorized",
                    "meaning": gt or "Ground truth not provided"
                })
    except Exception as e:
        print(f"❌ Error reading main CSV: {e}")
        import traceback
        traceback.print_exc()
    print(f"✅ Loaded {len(data)} main queries")
    return data

def machine_only(query, query_num=None, total=None):
    """Baseline: no RAG."""
    try:
        if query_num and total:
            print(f"  🤖 Calling Ollama for query {query_num}/{total}...")
        else:
            print(f"  🤖 Calling Ollama...")
        response = ollama.chat(
            model="mistral:7b",
            messages=[{"role": "user", "content": query}]
        )
        return response["message"]["content"]
    except Exception as e:
        print(f"  ❌ Ollama error: {e}")
        return f"[Ollama error: {e}]"

# ---------- Main evaluation ----------
def evaluate(dataset_type="main"):
    print("\n" + "="*70)
    print(f"STARTING EVALUATION: {dataset_type.upper()} DATASET (RAG_MODE={RAG_MODE})")
    print("="*70)

    if embedder is None:
        print("❌ Cannot run evaluation: sentence-transformers not available")
        return

    # Load dataset
    if dataset_type == "control":
        dataset = load_control_queries()
        results_file = f"control_{RAG_MODE}_results.csv"
        acc_file = f"control_{RAG_MODE}_accuracy.csv"
    elif dataset_type == "main":
        dataset = load_main_queries()
        results_file = f"main_200_{RAG_MODE}_results.csv"
        acc_file = f"main_200_{RAG_MODE}_accuracy.csv"
    else:
        dataset = load_queries()
        results_file = f"local_{RAG_MODE}_results.csv"
        acc_file = f"local_{RAG_MODE}_accuracy.csv"

    if not dataset:
        print("❌ No queries loaded – aborting.")
        return

    total = len(dataset)
    print(f"📊 Total queries: {total}\n")

    candidate_correct = 0
    candidate_incorrect = 0
    type_correct = {}
    type_total = {}
    times = []
    results_table = []
    retrieval_hits = 0
    total_retrieved = 0
    total_context_length = 0

    for idx, item in enumerate(dataset, 1):
        query = item["query"]
        gt = item["meaning"]
        ambiguity = item["ambiguity"]

        type_total[ambiguity] = type_total.get(ambiguity, 0) + 1

        print(f"\n{'='*70}")
        print(f"📝 QUERY {idx}/{total}")
        print(f"   Text: {query[:100]}{'...' if len(query)>100 else ''}")
        print(f"   Ambiguity: {ambiguity}")
        print(f"   Ground Truth: {gt[:100]}{'...' if len(gt)>100 else ''}")

        start = time.time()
        prediction, retrieved_context, retrieved_count = run_model(query, ambiguity)
        elapsed = time.time() - start
        times.append(elapsed)

        # Compute metrics
        context_length = len(retrieved_context.split()) if retrieved_context else 0
        retrieved_flag = 1 if retrieved_count > 0 else 0
        if retrieved_flag:
            retrieval_hits += 1
        total_retrieved += retrieved_count
        total_context_length += context_length

        # Similarity
        try:
            gt_emb = embedder.encode(gt, convert_to_tensor=True)
            pred_emb = embedder.encode(prediction, convert_to_tensor=True)
            sim = util.cos_sim(gt_emb, pred_emb)[0][0].item()
        except Exception as e:
            print(f"  ❌ Similarity error: {e}")
            sim = 0.0

        # Candidate correctness
        if sim > 0.65:
            candidate_correct += 1
            type_correct[ambiguity] = type_correct.get(ambiguity, 0) + 1
            status = "✅ CANDIDATE CORRECT"
        else:
            candidate_incorrect += 1
            status = "❌ CANDIDATE INCORRECT"

        print(f"   Similarity: {sim:.4f} | {status}")
        print(f"   ⏱️  {elapsed:.2f}s")
        if RAG_MODE in ["knowledge", "hybrid"]:
            print(f"   Retrieved facts: {retrieved_count}, Context length: {context_length} words")

        # Prepare row – for non-retrieval modes, set fields to "N/A"
        if RAG_MODE in ["machine", "ambiguity"]:
            row_flag = "N/A"
            row_count = "N/A"
            row_len = "N/A"
            row_context = "N/A"
        else:
            row_flag = retrieved_flag
            row_count = retrieved_count
            row_len = context_length
            row_context = retrieved_context[:200] + "..." if len(retrieved_context) > 200 else retrieved_context

        results_table.append([
            query,
            ambiguity,
            row_flag,
            row_count,
            row_len,
            row_context,
            gt,
            prediction,          # full prediction (no truncation)
            round(sim, 4),
            "",   # Expert1
            "",   # Expert2
            ""    # Final Label
        ])

        print(f"📊 Progress: {idx}/{total} | Correct: {candidate_correct} | Incorrect: {candidate_incorrect}")

    # Final stats
    accuracy = candidate_correct / total if total > 0 else 0
    avg_time = sum(times) / len(times) if times else 0
    print("\n" + "="*70)
    print("📊 FINAL RESULTS")
    print(f"   Candidate Accuracy: {accuracy:.4f} ({accuracy*100:.2f}%)")
    print(f"   Avg time per query: {avg_time:.2f}s")
    if RAG_MODE in ["knowledge", "hybrid"]:
        coverage = retrieval_hits / total if total > 0 else 0
        mean_retrieved = total_retrieved / total if total > 0 else 0
        mean_len = total_context_length / total if total > 0 else 0
        print(f"   Retrieval Coverage: {coverage:.4f} ({retrieval_hits}/{total})")
        print(f"   Mean Retrieved Facts: {mean_retrieved:.2f}")
        print(f"   Mean Context Length: {mean_len:.2f} words")

    # Save CSV
    print("\n💾 Saving results...")
    try:
        with open(results_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Query", "Ambiguity Type", "Retrieved", "Retrieved Count",
                "Context Length", "Retrieved Context",
                "Ground Truth", "Prediction", "Similarity",
                "Expert1", "Expert2", "Final Label"
            ])
            writer.writerows(results_table)
        print(f"   ✅ Saved: {results_file}")
    except Exception as e:
        print(f"   ❌ Error saving: {e}")

    print("="*70 + "\n")

# ---------- Main ----------
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 STARTING EVALUATION PIPELINE")
    print("="*70)
    print(f"⏰ Start time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Run ALL modes in sequence: knowledge, hybrid, machine, ambiguity
    # This order lets you stop after knowledge & hybrid if you already have machine & ambiguity
    modes = ["knowledge", "hybrid", "machine", "ambiguity"]
    
    for mode in modes:
        RAG_MODE = mode
        PREFIX = mode
        print("\n" + "="*70)
        print(f"📌 EXPERIMENT: MAIN DATASET with RAG_MODE = '{mode}'")
        print("="*70)
        evaluate(dataset_type="main")
        
        # Also run control dataset for each mode
        print("\n" + "="*70)
        print(f"📌 EXPERIMENT: CONTROL DATASET with RAG_MODE = '{mode}'")
        print("="*70)
        evaluate(dataset_type="control")

    if driver:
        driver.close()
        print("🔌 Neo4j connection closed")

    print("\n" + "="*70)
    print("✅ ALL EVALUATIONS COMPLETE")
    print("="*70)
    print(f"⏰ End time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n📂 Generated files (main dataset):")
    for mode in ["knowledge", "hybrid", "machine", "ambiguity"]:
        print(f"   - main_200_{mode}_results.csv")
        print(f"   - main_200_{mode}_accuracy.csv")
    print("\n📂 Generated files (control dataset):")
    for mode in ["knowledge", "hybrid", "machine", "ambiguity"]:
        print(f"   - control_{mode}_results.csv")
        print(f"   - control_{mode}_accuracy.csv")
    print("="*70)
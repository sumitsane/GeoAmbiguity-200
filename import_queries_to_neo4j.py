from neo4j import GraphDatabase
import csv
import os
import time
from datetime import datetime

# ─────────────────────────────────────────
# Neo4j Connection
# ─────────────────────────────────────────
def get_driver():
    """Create and test Neo4j connection"""
    try:
        driver = GraphDatabase.driver(
            "bolt://localhost:7687",
            auth=("neo4j", "Nadmin123")
        )
        with driver.session() as session:
            result = session.run("RETURN 1")
            result.single()
        print("✅ Neo4j connection established successfully")
        return driver
    except Exception as e:
        print(f"❌ Neo4j connection error: {e}")
        return None

# ─────────────────────────────────────────
# Clear existing data (optional)
# ─────────────────────────────────────────
def clear_database(driver, confirm=False):
    """Clear all GeoQuery, AmbiguityType, and IntendedInterpretation nodes"""
    if not confirm:
        response = input("⚠️  This will delete ALL GeoQuery, AmbiguityType, and IntendedInterpretation nodes. Continue? (yes/no): ")
        if response.lower() != 'yes':
            print("❌ Operation cancelled")
            return False
    
    try:
        with driver.session() as session:
            # Delete relationships first, then nodes
            session.run("""
                MATCH (q:GeoQuery)-[r:HAS_AMBIGUITY]->(a:AmbiguityType)
                DELETE r
            """)
            session.run("""
                MATCH (q:GeoQuery)-[r:INTENDED_MEANING]->(i:IntendedInterpretation)
                DELETE r
            """)
            session.run("MATCH (q:GeoQuery) DELETE q")
            session.run("MATCH (a:AmbiguityType) DELETE a")
            session.run("MATCH (i:IntendedInterpretation) DELETE i")
        print("✅ Database cleared successfully")
        return True
    except Exception as e:
        print(f"❌ Error clearing database: {e}")
        return False

# ─────────────────────────────────────────
# Import queries from CSV (FIXED)
# ─────────────────────────────────────────
def import_queries_from_csv(driver, csv_path="200queries.csv", clear_first=False):
    """
    Import queries from CSV into Neo4j with FIXED Cypher syntax
    """
    
    if not os.path.exists(csv_path):
        print(f"❌ CSV file not found: {csv_path}")
        return False
    
    if clear_first:
        if not clear_database(driver, confirm=False):
            return False
    
    print(f"\n📂 Reading CSV file: {csv_path}")
    
    # Read CSV data
    queries = []
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            row_count = 0
            for row in reader:
                row_count += 1
                query = row.get('Query', '').strip()
                primary_ambiguity = row.get('Primary Category', '').strip()
                secondary_categories = row.get('Secondary Categories', '').strip()
                ground_truth = row.get('Ground Truth', '').strip()
                
                if not query:
                    print(f"⚠️  Skipping row {row_count}: Empty query")
                    continue
                
                # Parse ambiguity types
                ambiguity_types = [primary_ambiguity] if primary_ambiguity else []
                if secondary_categories and secondary_categories != '—':
                    for sec in secondary_categories.split(','):
                        sec = sec.strip()
                        if sec:
                            ambiguity_types.append(sec)
                
                ambiguity_types = list(dict.fromkeys(ambiguity_types))
                
                queries.append({
                    'query': query,
                    'ambiguity_types': ambiguity_types,
                    'meaning': ground_truth or "Ground truth not provided"
                })
                
                if row_count % 50 == 0:
                    print(f"  Processed {row_count} rows...")
    
    except Exception as e:
        print(f"❌ Error reading CSV: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    print(f"✅ Loaded {len(queries)} queries from CSV")
    
    if not queries:
        print("❌ No queries to import")
        return False
    
    # ─────────────────────────────────────────
    # Import into Neo4j (FIXED)
    # ─────────────────────────────────────────
    print(f"\n⏳ Importing {len(queries)} queries into Neo4j...")
    
    success_count = 0
    error_count = 0
    
    try:
        with driver.session() as session:
            # Create constraints for uniqueness
            try:
                session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (q:GeoQuery) REQUIRE q.text IS UNIQUE")
                session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (a:AmbiguityType) REQUIRE a.name IS UNIQUE")
                session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (i:IntendedInterpretation) REQUIRE i.meaning IS UNIQUE")
                print("✅ Unique constraints created")
            except Exception as e:
                print(f"⚠️  Note: Could not create constraints (they may already exist): {e}")
            
            for idx, item in enumerate(queries, 1):
                try:
                    query_text = item['query']
                    ambiguity_types = item['ambiguity_types']
                    meaning = item['meaning']
                    
                    # ── Create/merge GeoQuery and IntendedInterpretation ──
                    # Using UNWIND and proper WITH clauses
                    result = session.run("""
                        MERGE (q:GeoQuery {text: $query_text})
                        WITH q
                        MERGE (i:IntendedInterpretation {meaning: $meaning})
                        WITH q, i
                        MERGE (q)-[:INTENDED_MEANING]->(i)
                        RETURN q.text as query_text
                    """, query_text=query_text, meaning=meaning)
                    
                    query_node = result.single()
                    
                    if not query_node:
                        print(f"❌ Failed to create query {idx}: {query_text[:50]}...")
                        error_count += 1
                        continue
                    
                    # ── Create ambiguity relationships ──
                    for amb_type in ambiguity_types:
                        try:
                            # FIXED: Use WITH to pass variables between MERGE and MATCH
                            session.run("""
                                MERGE (a:AmbiguityType {name: $amb_type})
                                WITH a
                                MATCH (q:GeoQuery {text: $query_text})
                                WITH q, a
                                MERGE (q)-[:HAS_AMBIGUITY]->(a)
                            """, amb_type=amb_type, query_text=query_text)
                        except Exception as e:
                            print(f"⚠️  Error creating ambiguity type '{amb_type}' for query {idx}: {e}")
                    
                    success_count += 1
                    
                    if idx % 10 == 0:
                        print(f"  Imported {idx}/{len(queries)} queries...")
                
                except Exception as e:
                    print(f"❌ Error importing query {idx}: {e}")
                    error_count += 1
    
    except Exception as e:
        print(f"❌ Database error during import: {e}")
        return False
    
    # ─────────────────────────────────────────
    # Verify import
    # ─────────────────────────────────────────
    print(f"\n📊 Import Summary:")
    print(f"   ✅ Successfully imported: {success_count} queries")
    if error_count > 0:
        print(f"   ❌ Failed: {error_count} queries")
    
    try:
        with driver.session() as session:
            query_count = session.run("MATCH (q:GeoQuery) RETURN count(q) as count").single()['count']
            ambiguity_count = session.run("MATCH (a:AmbiguityType) RETURN count(a) as count").single()['count']
            meaning_count = session.run("MATCH (i:IntendedInterpretation) RETURN count(i) as count").single()['count']
            
            print(f"\n📊 Database Statistics:")
            print(f"   GeoQuery nodes: {query_count}")
            print(f"   AmbiguityType nodes: {ambiguity_count}")
            print(f"   IntendedInterpretation nodes: {meaning_count}")
            
            # Show sample
            print(f"\n📝 Sample imported queries (first 3):")
            results = session.run("""
                MATCH (q:GeoQuery)-[:HAS_AMBIGUITY]->(a:AmbiguityType)
                MATCH (q)-[:INTENDED_MEANING]->(i:IntendedInterpretation)
                RETURN q.text as query, collect(a.name) as ambiguities, i.meaning as meaning
                LIMIT 3
            """)
            for r in results:
                print(f"\n   Query: {r['query'][:80]}...")
                print(f"   Ambiguities: {', '.join(r['ambiguities'])}")
                print(f"   Meaning: {r['meaning'][:80]}...")
            
    except Exception as e:
        print(f"⚠️  Could not verify import: {e}")
    
    return success_count > 0

# ─────────────────────────────────────────
# Export current database (for verification)
# ─────────────────────────────────────────
def export_database(driver, output_path="neo4j_export.csv"):
    """Export all queries from Neo4j to CSV for verification"""
    print(f"\n⏳ Exporting database to {output_path}...")
    
    try:
        with driver.session() as session:
            results = session.run("""
                MATCH (q:GeoQuery)-[:HAS_AMBIGUITY]->(a:AmbiguityType)
                MATCH (q)-[:INTENDED_MEANING]->(i:IntendedInterpretation)
                RETURN q.text as query, collect(a.name) as ambiguity_types, i.meaning as meaning
                ORDER BY q.text
            """)
            
            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['Query', 'Ambiguity Types', 'Ground Truth'])
                
                count = 0
                for r in results:
                    writer.writerow([
                        r['query'],
                        '; '.join(r['ambiguity_types']),
                        r['meaning']
                    ])
                    count += 1
            
            print(f"✅ Exported {count} queries to {output_path}")
            return True
    
    except Exception as e:
        print(f"❌ Export error: {e}")
        return False

# ─────────────────────────────────────────
# Test query
# ─────────────────────────────────────────
def test_query(driver, query_text="Where is Trikut Buru?"):
    """Test a query against the database"""
    print(f"\n🔍 Testing query: {query_text}")
    
    try:
        with driver.session() as session:
            result = session.run("""
                MATCH (q:GeoQuery {text: $query_text})-[:HAS_AMBIGUITY]->(a:AmbiguityType)
                MATCH (q)-[:INTENDED_MEANING]->(i:IntendedInterpretation)
                RETURN q.text as query, collect(a.name) as ambiguities, i.meaning as meaning
            """, query_text=query_text)
            
            row = result.single()
            if row:
                print(f"   ✅ Found query in database:")
                print(f"      Query: {row['query']}")
                print(f"      Ambiguities: {', '.join(row['ambiguities'])}")
                print(f"      Meaning: {row['meaning']}")
                return True
            else:
                print(f"   ❌ Query not found: {query_text}")
                return False
    
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

# ─────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────
def main():
    print("\n" + "="*70)
    print("🚀 NEO4J QUERY IMPORT SCRIPT (FIXED)")
    print("="*70)
    print(f"⏰ Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)
    
    driver = get_driver()
    if not driver:
        print("❌ Cannot proceed without Neo4j connection")
        return
    
    try:
        csv_file = "200queries.csv"
        if not os.path.exists(csv_file):
            print(f"\n❌ CSV file '{csv_file}' not found in current directory!")
            print(f"   Current directory: {os.getcwd()}")
            return
        
        print("\n📋 Import Options:")
        print("  1. Import fresh (clear existing data first)")
        print("  2. Append to existing data (MERGE, no duplicates)")
        print("  3. Preview only (no import)")
        print("  4. Export current database")
        print("  5. Test query")
        print("  6. Exit")
        
        choice = input("\nSelect option (1-6): ").strip()
        
        if choice == '1':
            import_queries_from_csv(driver, csv_file, clear_first=True)
        
        elif choice == '2':
            import_queries_from_csv(driver, csv_file, clear_first=False)
        
        elif choice == '3':
            print(f"\n📂 Previewing CSV: {csv_file}")
            with open(csv_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                count = 0
                for row in reader:
                    count += 1
                    if count <= 5:
                        print(f"\n  Query {count}:")
                        print(f"    Query: {row.get('Query', '')[:80]}...")
                        print(f"    Primary Category: {row.get('Primary Category', '')}")
                        print(f"    Secondary Categories: {row.get('Secondary Categories', '')}")
                        print(f"    Ground Truth: {row.get('Ground Truth', '')[:80]}...")
                print(f"\n  Total rows in CSV: {count}")
        
        elif choice == '4':
            export_database(driver)
        
        elif choice == '5':
            test_q = input("Enter a query to test: ").strip()
            if test_q:
                test_query(driver, test_q)
            else:
                print("❌ No query provided")
        
        elif choice == '6':
            print("👋 Exiting...")
        
        else:
            print("❌ Invalid choice")
    
    except KeyboardInterrupt:
        print("\n\n⏹️  Interrupted by user")
    
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        if driver:
            driver.close()
            print("\n🔌 Neo4j connection closed")
    
    print("\n" + "="*70)
    print("✅ SCRIPT COMPLETE")
    print("="*70)

if __name__ == "__main__":
    main()
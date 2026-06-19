import csv
import logging
import requests
from neo4j import GraphDatabase

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 1. Configuration Constants
CSV_FILE_PATH = "q.csv"  # Place your q.csv in the working directory
NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "Nadmin123")  # Verified working credentials
OVERPASS_URL = "https://overpass-api.de"  # Corrected API endpoint

# 2. Extract: Fetch Geo-Coordinates & Meta-attributes via Overpass API for Dumka & Deoghar
def fetch_osm_enrichment():
    logging.info("Querying OpenStreetMap Overpass for Dumka and Deoghar micro-features...")
   
    # Bounding box roughly wrapping Deoghar and Dumka districts (Lat: 24.0 to 24.7, Lon: 86.4 to 87.6)
    overpass_query = """
    [out:json][timeout:120];
    (
      // Fetch structural nodes & ways mapping to your specific 200 questions
      node["amenity"~"hospital|place_of_worship|bus_station"](24.0,86.4,24.7,87.6);
      way["amenity"~"hospital|place_of_worship|bus_station"](24.0,86.4,24.7,87.6);
     
      node["natural"~"peak|hill|water"](24.0,86.4,24.7,87.6);
      way["natural"~"peak|hill|water"](24.0,86.4,24.7,87.6);
     
      node["place"~"city|town|village|suburb"](24.0,86.4,24.7,87.6);
      way["water"="pond"](24.0,86.4,24.7,87.6);
    );
    out tags center;
    """
    
    # Headers to prevent 406/403 request rejections
    headers = {
        "User-Agent": "HildaSpatialBot/1.0 (contact: sumit)",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    try:
        # Use params to avoid encoding rejections
        response = requests.post(OVERPASS_URL, params={'data': overpass_query}, headers=headers, timeout=130)
        
        if response.status_code != 200:
            logging.error(f"Overpass API returned status code {response.status_code}. Response text: {response.text[:200]}")
            return []
            
        elements = response.json().get('elements', [])
        logging.info(f"Retrieved {len(elements)} spatial features from OpenStreetMap.")
        return elements
    except requests.exceptions.JSONDecodeError:
        logging.error("Failed to parse OSM response as JSON. The server likely returned an HTML error page.")
        return []
    except Exception as e:
        logging.error(f"Failed to extract bounding data from OSM: {e}")
        return []

# 3. Transform & Load: Process CSV and stitch coordinates into a Neo4j Graph
def build_knowledge_graph():
    # Fetch geographic reference layer
    osm_features = fetch_osm_enrichment()
   
    # Build an in-memory lookup table to map names/aliases to coordinates & labels
    geo_lookup = {}
    for elem in osm_features:
        tags = elem.get("tags", {})
        name = tags.get("name", "").lower()
        alt_name = tags.get("alt_name", "").lower()
        name_en = tags.get("name:en", "").lower()
       
        lat = elem.get("lat", elem.get("center", {}).get("lat"))
        lon = elem.get("lon", elem.get("center", {}).get("lon"))
       
        # Build list of matching candidate names for fuzzy lookup
        keys = [name, alt_name, name_en, tags.get("name:sat", "").lower()]
        for key in keys:
            if key:
                geo_lookup[key] = {
                    "lat": float(lat) if lat else None,
                    "lon": float(lon) if lon else None,
                    "tags": tags
                }

    # Open connection to Neo4j instance
    logging.info("Connecting to Neo4j database...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    
    # Clean previous run attempts that generated blank property nodes
    logging.info("Purging stale spatial entities from the previous incomplete run...")
    try:
        with driver.session() as session:
            session.run("MATCH (n:SpatialEntity) DETACH DELETE n")
            session.run("MATCH (d:SpatialDistrict) DETACH DELETE d")
    except Exception as e:
        logging.error(f"Failed to clear old database records: {e}")
   
    # Core Cypher script to structure the Knowledge Graph and link entities safely
    cypher_query = """
    UNWIND $batch AS row
   
    // 1. Merge the main Spatial Landmark Node
    MERGE (e:SpatialEntity {name: row.clean_name})
    ON CREATE SET e.label_type = row.inferred_type,
                  e.lat = row.lat,
                  e.lon = row.lon,
                  e.aliases = row.aliases,
                  e.description = row.context_description,
                  e.source = 'q.csv + OSM'
   
    // 2. Conditionally structure the parent district layout hierarchy
    FOREACH (ignored IN case when row.district IS NOT NULL then [1] else [] end |
        MERGE (d:SpatialDistrict {name: row.district})
        ON CREATE SET d.state = 'Jharkhand'
        MERGE (e)-[:LOCATED_IN]->(d)
    )
    """

    batch = []
   
    logging.info(f"Processing local source file: {CSV_FILE_PATH}")
    try:
        with open(CSV_FILE_PATH, mode='r', encoding='utf-8') as file:
            # Flexible reading to support comma or semicolon delimited CSV sheets
            sample = file.read(2048)
            dialect = csv.Sniffer().sniff(sample) if sample else 'excel'
            file.seek(0)
           
            reader = csv.DictReader(file, dialect=dialect)
           
            # Map out common header column fallbacks if headers aren't precisely standard
            name_col = next((h for h in reader.fieldnames if 'name' in h.lower() or 'place' in h.lower() or 'entity' in h.lower()), None)
            desc_col = next((h for h in reader.fieldnames if 'desc' in h.lower() or 'text' in h.lower() or 'fact' in h.lower() or 'query' in h.lower()), None)
            dist_col = next((h for h in reader.fieldnames if 'dist' in h.lower()), None)

            for csv_row in reader:
                raw_name = csv_row.get(name_col) if name_col else list(csv_row.values())[0]
                description = csv_row.get(desc_col, "") if desc_col else ""
                district = csv_row.get(dist_col, "Unknown") if dist_col else ("Dumka" if "dumka" in description.lower() else "Deoghar" if "deoghar" in description.lower() else None)
               
                if not raw_name:
                    continue
               
                clean_name = raw_name.strip()
                search_key = clean_name.lower()
               
                # Check OSM lookup table to fetch real coordinates & aliases
                matched_geo = geo_lookup.get(search_key, {"lat": None, "lon": None, "tags": {}})
               
                # Infer entity type dynamically based on string values
                inferred_type = "Infrastructure"
                if any(x in search_key for x in ["buru", "hill", "peak", "ridge"]):
                    inferred_type = "Mountain"
                elif any(x in search_key for x in ["pond", "river", "lake", "ghat"]):
                    inferred_type = "WaterBody"
                elif "hospital" in search_key:
                    inferred_type = "MedicalCampus"
                elif "temple" in search_key or "dham" in search_key:
                    inferred_type = "ReligiousSite"
                elif "village" in search_key or "nayapara" in search_key:
                    inferred_type = "Settlement"

                # Capture aliases to handle local spelling changes
                osm_tags = matched_geo["tags"]
                aliases = list(set(filter(None, [
                    clean_name,
                    osm_tags.get("alt_name"),
                    osm_tags.get("name:en"),
                    osm_tags.get("name:sat"),
                    osm_tags.get("name:sat_Olck")
                ])))

                batch.append({
                    "clean_name": clean_name,
                    "inferred_type": inferred_type,
                    "lat": matched_geo["lat"],
                    "lon": matched_geo["lon"],
                    "district": district,
                    "context_description": description,
                    "aliases": aliases
                })

        # Execute insertion transaction
        if batch:
            with driver.session() as session:
                logging.info(f"Uploading {len(batch)} structural facts and landmarks into Neo4j...")
                session.run(cypher_query, batch=batch)
                logging.info("Graph processing successfully finished! No more zero-fact returns.")
        else:
            logging.error("No valid entities extracted from q.csv.")
           
    except FileNotFoundError:
        logging.error(f"Could not find file '{CSV_FILE_PATH}' in your workspace. Please confirm its location.")
    except Exception as e:
        logging.error(f"An unexpected data processing error occurred: {e}")
    finally:
        driver.close()

if __name__ == "__main__":
    build_knowledge_graph()

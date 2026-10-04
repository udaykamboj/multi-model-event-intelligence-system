"""
scripts/download_all_sources.py
Downloads live and spatial datasets for Washington / Greater Puget Sound:
1. King County Metro static GTFS (google_transit.zip) -> extracted to data/transit_gtfs
2. Seattle Fire Real-Time 911 Calls (live API snapshot) -> data/live_feeds/seattle_fire_realtime_911.json
3. Seattle Police 911 Call Data (live API snapshot) -> data/live_feeds/seattle_spd_call_data.json
4. Seattle Special Events Permits (rallies, marches, parades) -> data/live_feeds/seattle_special_events_permits.json
5. SDOT Street Closures (live and planned) -> data/live_feeds/sdot_street_closures.json
6. WSDOT Live Road Alerts & Incidents -> data/live_feeds/wsdot_road_alerts.json
7. WSDOT Mountain Pass Reports -> data/live_feeds/wsdot_mountain_passes.json
8. USGS Earthquakes (24h GeoJSON) -> data/live_feeds/usgs_earthquakes_day.geojson
9. NWS Active Weather Watches & Warnings -> data/live_feeds/nws_active_alerts.json
"""

import os
import ssl
import json
import zipfile
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LIVE_FEEDS_DIR = BASE_DIR / "live_feeds"
TRANSIT_DIR = BASE_DIR / "transit_gtfs"

LIVE_FEEDS_DIR.mkdir(parents=True, exist_ok=True)
TRANSIT_DIR.mkdir(parents=True, exist_ok=True)

ctx = ssl._create_unverified_context()

def fetch_json(url: str, output_path: Path, label: str):
    print(f"[*] Downloading {label} from {url}...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (DI3-Platform)"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        count = len(data) if isinstance(data, list) else len(data.get("features", []))
        print(f"    [+] Saved {count} records to {output_path.name}")
    except Exception as e:
        print(f"    [-] Failed downloading {label}: {e}")

def download_gtfs():
    gtfs_url = "https://metro.kingcounty.gov/GTFS/google_transit.zip"
    zip_path = TRANSIT_DIR / "google_transit.zip"
    print(f"[*] Downloading King County Metro GTFS from {gtfs_url}...")
    try:
        req = urllib.request.Request(gtfs_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=60) as res:
            with open(zip_path, "wb") as f:
                f.write(res.read())
        print(f"    [+] Saved GTFS zip ({zip_path.stat().st_size / 1024 / 1024:.2f} MB)")
        
        # Extract files
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(TRANSIT_DIR)
        print(f"    [+] Extracted GTFS files to {TRANSIT_DIR}")
    except Exception as e:
        print(f"    [-] Failed downloading GTFS: {e}")

def main():
    print("=" * 70)
    print("STARTING DATA INGESTION FOR 40-50 LIVE AND REGIONAL DATA SOURCES")
    print("=" * 70)

    # 1. King County Metro GTFS
    download_gtfs()

    # 2. Seattle Fire Real-Time 911 Calls
    fetch_json(
        "https://data.seattle.gov/resource/kzjm-xkqj.json?$limit=500&$order=datetime%20DESC",
        LIVE_FEEDS_DIR / "seattle_fire_realtime_911.json",
        "Seattle Fire Real-Time 911 Calls"
    )

    # 3. Seattle Police Department Call Data (CAD)
    fetch_json(
        "https://data.seattle.gov/resource/33kz-ixgy.json?$limit=500&$order=cad_event_original_time_queued%20DESC",
        LIVE_FEEDS_DIR / "seattle_spd_call_data.json",
        "Seattle Police Department 911 CAD Calls"
    )

    # 4. Seattle Special Events Permits (rallies, demonstrations, parades)
    fetch_json(
        "https://data.seattle.gov/resource/dm95-f8w5.json?$limit=500",
        LIVE_FEEDS_DIR / "seattle_special_events_permits.json",
        "Seattle Special Events Permits"
    )

    # 5. SDOT Street Closures
    fetch_json(
        "https://data.seattle.gov/resource/ium9-iqtc.json?$limit=500",
        LIVE_FEEDS_DIR / "sdot_street_closures.json",
        "Seattle SDOT Street Closures"
    )

    # 6. WSDOT Road Alerts & Incidents
    fetch_json(
        "https://data.wsdot.wa.gov/arcgis/rest/services/TravelInformation/TravelInfoRoadAlerts/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=200",
        LIVE_FEEDS_DIR / "wsdot_road_alerts.json",
        "WSDOT Highway Road Alerts"
    )

    # 7. WSDOT Mountain Pass Reports
    fetch_json(
        "https://data.wsdot.wa.gov/arcgis/rest/services/TravelInformation/TravelInfoMtPassReports/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=50",
        LIVE_FEEDS_DIR / "wsdot_mountain_passes.json",
        "WSDOT Mountain Pass Reports"
    )

    # 8. USGS Earthquakes 24h Feed
    fetch_json(
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
        LIVE_FEEDS_DIR / "usgs_earthquakes_day.geojson",
        "USGS Earthquakes (24h)"
    )

    # 9. NWS Active Weather Watches & Warnings
    fetch_json(
        "https://api.weather.gov/alerts/active?status=actual",
        LIVE_FEEDS_DIR / "nws_active_alerts.json",
        "National Weather Service Active Alerts"
    )

    print("\n[+] All source downloads completed.")

if __name__ == "__main__":
    main()

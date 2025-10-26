import requests
import json
import sys
from datetime import datetime
import os

# Configuration - Access secrets via environment variables
MBTA_API_KEY = os.getenv('MBTA_API_KEY', '')  # Optional
INFLUX_URL = os.getenv('INFLUX_URL')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN')
INFLUX_ORG = os.getenv('INFLUX_ORG')
INFLUX_BUCKET = os.getenv('INFLUX_BUCKET')

# Validate environment variables
if not all([INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET]):
    print("✗ Missing required InfluxDB environment variables")
    sys.exit(1)

# MBTA Vehicles API endpoint
MBTA_URL = "https://api-v3.mbta.com/vehicles?include=stop,trip&sort=updated_at"
if MBTA_API_KEY:
    MBTA_URL += f"&api_key={MBTA_API_KEY}"

print("Fetching MBTA vehicle locations...")

# 1. Fetch MBTA vehicle data
try:
    response = requests.get(MBTA_URL, timeout=10)
    response.raise_for_status()
except requests.RequestException as e:
    print(f"✗ API error: {str(e)}")
    sys.exit(1)

data = response.json()
vehicles = data.get('data', [])
included = data.get('included', [])

if not vehicles:
    print("✗ No vehicles found")
    sys.exit(1)

print(f"✓ Found {len(vehicles)} vehicles")

# Initialize run summary
run_time = int(datetime.utcnow().timestamp())
vehicles_attempted = len(vehicles)
vehicles_passed = 0
vehicles_failed = 0

# 2. Build lookup maps for stops and trips
stops = {item['id']: item['attributes']['name'] for item in included if item['type'] == 'stop' and 'id' in item}
trips = {item['id']: item['attributes']['headsign'] for item in included if item['type'] == 'trip' and 'id' in item}

# 3. Transform to InfluxDB Line Protocol
line_protocol_lines = []
skipped_vehicles = []
for vehicle in vehicles:
    try:
        attrs = vehicle.get('attributes', {})
        vehicle_id = vehicle.get('id', 'NA')
        
        # Safely handle relationships
        rels = vehicle.get('relationships') or {}
        
        # Validate relationships with nested checks
        route_id = 'NA'
        if isinstance(rels.get('route'), dict) and isinstance(rels['route'].get('data'), dict):
            route_id = rels['route']['data'].get('id', 'NA')
        
        trip_id = 'NA'
        if isinstance(rels.get('trip'), dict) and isinstance(rels['trip'].get('data'), dict):
            trip_id = rels['trip']['data'].get('id', 'NA')
        
        stop_id = 'NA'
        if isinstance(rels.get('stop'), dict) and isinstance(rels['stop'].get('data'), dict):
            stop_id = rels['stop']['data'].get('id', 'NA')
        
        # Skip if critical data is missing
        if vehicle_id == 'NA' and route_id == 'NA' and trip_id == 'NA' and stop_id == 'NA':
            skipped_vehicles.append(f"Vehicle {vehicle_id}: Missing all critical data (route={route_id}, trip={trip_id}, stop={stop_id})")
            vehicles_failed += 1
            continue
        
        direction_id = str(attrs.get('direction_id', 'NA')) if attrs.get('direction_id') is not None else 'NA'
        current_status = attrs.get('current_status', 'NA')
        stop_name = stops.get(stop_id, 'NA')
        headsign = trips.get(trip_id, 'NA')
        
        # Use updated_at as timestamp (convert to Unix seconds)
        updated_at_str = attrs.get('updated_at')
        if updated_at_str:
            try:
                updated_at = int(datetime.fromisoformat(updated_at_str.rstrip('Z')).timestamp())
            except ValueError as e:
                skipped_vehicles.append(f"Vehicle {vehicle_id}: Invalid timestamp format ({updated_at_str}, error: {str(e)})")
                vehicles_failed += 1
                continue
        else:
            updated_at = int(datetime.utcnow().timestamp())
            skipped_vehicles.append(f"Vehicle {vehicle_id}: Missing updated_at, using current time ({updated_at})")
        
        # Tags (escaped for special characters)
        tags = [
            f"id={vehicle_id}",
            f"route_id={route_id}",
            f"trip_id={trip_id}",
            f"stop_id={stop_id}",
            f"direction_id={direction_id}",
            f"current_status={current_status}",
            f"stop_name=\"{stop_name.replace('\"', '\\\"').replace(',', '\\,').replace(' ', '_')}\"",
            f"headsign=\"{headsign.replace('\"', '\\\"').replace(',', '\\,').replace(' ', '_')}\""
        ]
        tags_str = ",".join(tags)
        
        # Fields: Only include non-None values
        fields_list = []
        for field, value in [
            ('latitude', attrs.get('latitude')),
            ('longitude', attrs.get('longitude')),
            ('bearing', attrs.get('bearing')),
            ('speed', attrs.get('speed')),
            ('position_latency', attrs.get('position_latency'))
        ]:
            if value is not None:
                try:
                    # Ensure numeric fields are valid
                    if field in ['latitude', 'longitude', 'bearing', 'speed', 'position_latency']:
                        float(value)  # Validate numeric
                    fields_list.append(f"{field}={value}")
                except (ValueError, TypeError) as e:
                    skipped_vehicles.append(f"Vehicle {vehicle_id}: Invalid field value for {field} ({value}, error: {str(e)})")
                    continue
        
        # Only create line if there are valid fields
        if fields_list:
            fields = ",".join(fields_list)
            line = f"mbta_vehicle,{tags_str} {fields} {updated_at}"
            # Validate Line Protocol format
            parts = line.split(' ')
            if len(parts) != 3 or not parts[0].startswith('mbta_vehicle,') or not fields or not tags_str:
                skipped_vehicles.append(f"Vehicle {vehicle_id}: Invalid Line Protocol (line={line}, tags={tags_str}, fields={fields}, raw_data={json.dumps(vehicle, indent=2)})")
                vehicles_failed += 1
                continue
            # Check for tag-like strings in fields
            if any(tag.split('=')[0] in fields for tag in tags):
                skipped_vehicles.append(f"Vehicle {vehicle_id}: Tag-field mixup detected (line={line}, tags={tags_str}, fields={fields}, raw_data={json.dumps(vehicle, indent=2)})")
                vehicles_failed += 1
                continue
            line_protocol_lines.append(line)
            vehicles_passed += 1
        else:
            skipped_vehicles.append(f"Vehicle {vehicle_id}: No valid fields to write (latitude={attrs.get('latitude')}, longitude={attrs.get('longitude')}, bearing={attrs.get('bearing')}, speed={attrs.get('speed')}, position_latency={attrs.get('position_latency')}, raw_data={json.dumps(vehicle, indent=2)})")
            vehicles_failed += 1
    
    except Exception as e:
        skipped_vehicles.append(f"Vehicle {vehicle_id}: Error processing vehicle data (error={str(e)}, raw_data={json.dumps(vehicle, indent=2)})")
        vehicles_failed += 1
        continue

# Log skipped vehicles
if skipped_vehicles:
    print(f"Skipped {len(skipped_vehicles)} vehicles:")
    for skip_msg in skipped_vehicles[:10]:  # Limit to 10 for brevity
        print(skip_msg)

# If no valid lines, log run summary and exit
if not line_protocol_lines:
    print("✗ No valid vehicle data to write to InfluxDB")
    # Still write run summary
    summary_line = f"mbta_run_summary vehicles_attempted={vehicles_attempted}i,vehicles_passed={vehicles_passed}i,vehicles_failed={vehicles_failed}i {run_time}"
    line_protocol = summary_line
else:
    # Add run summary to Line Protocol
    summary_line = f"mbta_run_summary vehicles_attempted={vehicles_attempted}i,vehicles_passed={vehicles_passed}i,vehicles_failed={vehicles_failed}i {run_time}"
    line_protocol = '\n'.join(line_protocol_lines + [summary_line])

# Debug: Print Line Protocol for inspection (all vehicle lines + run summary)
print("Line Protocol (all vehicle lines + run summary):")
for line in line_protocol.split('\n')[:10]:
    print(line)
print(f"Run Summary: {summary_line}")

# 4. Write to InfluxDB Cloud
write_url = f"{INFLUX_URL}/api/v2/write?org={INFLUX_ORG}&bucket={INFLUX_BUCKET}&precision=s"
headers = {
    "Authorization": f"Token {INFLUX_TOKEN}",
    "Content-Type": "text/plain; charset=utf-8"
}
try:
    write_response = requests.post(write_url, headers=headers, data=line_protocol, timeout=10)
    if write_response.status_code == 204:
        print(f"✓ Successfully wrote {len(line_protocol_lines)} vehicle locations and 1 run summary to InfluxDB")
    else:
        print(f"✗ Failed to write to InfluxDB: {write_response.status_code} - {write_response.text}")
        sys.exit(1)
except requests.RequestException as e:
    print(f"✗ InfluxDB write error: {str(e)}")
    sys.exit(1)
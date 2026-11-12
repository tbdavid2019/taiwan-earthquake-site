#!/usr/bin/env python3
"""Rebuild the earthquake CSV from the CWA's annual open-data catalogs."""
import csv
import io
import json
import math
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
BASE = 'https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/'
PUBLIC_KEY = 'rdec-key-123-45678-011121314'
NS = {'cwa': 'urn:cwa:gov:tw:cwacommon:0.1'}
FIELDS = ['Year', 'OriginTime', 'EpicenterLongitude', 'EpicenterLatitude',
          'FocalDepth', 'LocalMagnitude', 'Quality', 'ReviewStatus', 'InFileRegion']


def download(dataset, fmt):
    url = f'{BASE}{dataset}?Authorization={PUBLIC_KEY}&format={fmt}'
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def number_range(value):
    match = re.fullmatch(r'\s*([+-]?[\d.]+)\s*-\s*([+-]?[\d.]+)\s*', value)
    if not match:
        raise ValueError(f'Invalid catalog range: {value!r}')
    low, high = map(float, match.groups())
    if not math.isfinite(low) or not math.isfinite(high) or low > high:
        raise ValueError(f'Invalid catalog range: {value!r}')
    return low, high


def convert_catalog(year, info, events):
    if int(info['RecordNumber']) != len(events):
        raise ValueError(f'{year}: catalog record count does not match')
    min_lon, max_lon = number_range(info['Region']['LongitudeRange'])
    min_lat, max_lat = number_range(info['Region']['LatitudeRange'])
    rows = []
    for event in events:
        row = {field: str(event[field]).strip() for field in FIELDS[1:-1]}
        timestamp = datetime.fromisoformat(row['OriginTime'])
        if timestamp.tzinfo is None:
            raise ValueError(f'{year}: origin time has no timezone')
        lon, lat, depth, magnitude = (float(row[key]) for key in (
            'EpicenterLongitude', 'EpicenterLatitude', 'FocalDepth', 'LocalMagnitude'))
        if not all(map(math.isfinite, (lon, lat, depth, magnitude))) or magnitude < 3:
            raise ValueError(f'{year}: invalid earthquake values')
        if row['Quality'] not in ('A', 'B', 'C', 'D') or not row['ReviewStatus']:
            raise ValueError(f'{year}: invalid quality or review status')
        row['Year'] = year
        row['InFileRegion'] = str(min_lon <= lon <= max_lon and min_lat <= lat <= max_lat).lower()
        rows.append(row)
    return rows


def parse_xml(content, year):
    root = ET.fromstring(content)
    info = root.find('.//cwa:CatalogInfo', NS)
    if info is None:
        raise ValueError(f'{year}: missing catalog metadata')
    region = info.find('cwa:Region', NS)
    metadata = {
        'RecordNumber': info.findtext('cwa:RecordNumber', namespaces=NS),
        'Region': {name: region.findtext(f'cwa:{name}', namespaces=NS)
                   for name in ('LongitudeRange', 'LatitudeRange')},
    }
    events = [{field: event.findtext(f'cwa:{field}', namespaces=NS)
               for field in FIELDS[1:-1]}
              for event in root.findall('.//cwa:EarthquakeInfo', NS)]
    return convert_catalog(year, metadata, events)


def main():
    current = json.loads(download('E-A0073-001', 'JSON'))['cwaopendata']
    match = re.fullmatch(r'CWA-EQ-Catalog-(\d{4})', current['identifier'])
    if not match:
        raise ValueError('Unexpected current catalog identifier')
    current_year = int(match.group(1))
    catalog = current['Dataset']['Catalog']
    info = catalog['CatalogInfo']
    start = datetime.fromisoformat(info['StartTime'])
    end = datetime.fromisoformat(info['EndTime'])
    issued = datetime.fromisoformat(current['sent'])
    if any(value.tzinfo is None for value in (start, end, issued)) or end <= start:
        raise ValueError('Invalid current catalog time range')
    if start.year != current_year:
        raise ValueError('Current catalog year does not match its time range')
    events = catalog.get('EarthquakeInfo', [])
    if isinstance(events, dict):
        events = [events]
    current_rows = convert_catalog(current_year, info, events)
    rows = []
    with zipfile.ZipFile(io.BytesIO(download('E-A0073-002', 'ZIP'))) as archive:
        for year in range(1990, current_year):
            name = f'CWA-EQ-Catalog-{year}.xml'
            rows.extend(parse_xml(archive.read(name), year))
    rows.extend(current_rows)
    if not rows:
        raise ValueError('Refusing to publish an empty catalog')
    csv_buffer = io.StringIO(newline='')
    writer = csv.DictWriter(csv_buffer, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    metadata = {
        'current_catalog_year': current_year,
        'current_catalog_issued_at': current['sent'],
        'current_catalog_end_exclusive': info['EndTime'],
        'latest_event_time': max(row['OriginTime'] for row in rows),
        'record_count': len(rows),
    }
    # Construct both files completely before changing either of them.
    csv_path = DATA / 'earthquakes.csv'
    metadata_path = DATA / 'earthquakes-meta.json'
    csv_path.write_text(csv_buffer.getvalue(), encoding='utf-8')
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"Updated {len(rows):,} records; current catalog published {current['sent']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Catalog update failed: {error}', file=sys.stderr)
        sys.exit(1)

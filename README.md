# zwift-map-merge

Adds the map of a Zwift activity to the activity recorded by your watch, directly in Garmin Connect.

## The problem

During a Zwift run, the footpod is paired with the watch: the watch activity has all the data (heart rate, footpod, running dynamics, Training Effect) but no map. Zwift also sends its own activity to Garmin, with the map but poor data. So you keep the watch activity, delete the Zwift one, and lose the map.

## What it does

For each pair of *watch activity without a map* + *Zwift activity with a map* that overlap in time:

1. It downloads both original FIT files and backs them up in `data/backups/<id>/`.
2. It adds the Zwift GPS position (and, optionally, altitude) to the watch FIT, point by point, by timestamp. Everything else in the watch file is kept byte for byte: proprietary messages, developer fields, device info. The activity still counts as a watch activity.
3. It reads the merged file back to validate it (CRC, sizes, GPS points present).
4. **Only when you ask** (or in automatic mode), it deletes the Zwift copy and the original watch activity on Garmin, imports the merged file, then restores the name, description, type and gear.

If the import fails, the original watch FIT is re-imported. The backups are always kept.

Strava is never touched. Garmin does not share imported activities with Strava.

## Install (Docker compose)

```sh
mkdir zwift-map-merge && cd zwift-map-merge
curl -O https://raw.githubusercontent.com/Jeffs24/zwift-map-merge/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/Jeffs24/zwift-map-merge/main/.env.example
docker compose up -d
```

Open `http://SERVER:8126` and log in to Garmin (MFA codes are supported). Only the tokens are kept, in the `/data` volume.

## Usage

- The page lists the detected pairs (scan every 30 min over 7 days, or the “Scan now” button).
- **Simulate**: downloads and merges without changing anything on Garmin. Then **Preview** shows the merged activity as Garmin will get it: map, stats (time, distance, heart rate, cadence, power, laps, GPS points) and charts. The merged FIT can also be downloaded.
- **Merge**: replaces the activities on Garmin.
- **Ignore**: the pair is no longer offered.

### Older activities (Zwift copy already deleted from Garmin)

**Zwift account (automatic).** Connect your Zwift account in the “Zwift account” box, pick a start date and click **Sync history**. The tool lists your Zwift activities through Zwift's unofficial Companion API and downloads the FIT file of each one that overlaps a watch activity without a map; the others are skipped without being downloaded. Each match shows up in the list as a pair to simulate and merge, so nothing changes on Garmin until you click (or `AUTO_MERGE=true`). Activities already looked at are remembered, so a second sync only fetches new ones. Once connected, new Zwift activities are also fetched at each scan (`ZWIFT_AUTO_SYNC`).

Your Zwift password is not stored: only the OAuth tokens, in `/data/zwift_tokens.json`. Zwift has no public API, so this can break if Zwift changes its endpoints; uploading files by hand (below) keeps working.

**By hand.** Upload the Zwift FIT files in the “Older activities” box of the page (several at once). Each file is paired with the watch activity without a map that overlaps it, then shows up in the list like any other pair. Two ways to get these files:

- **zwift.com**: *My activities* → the activity → *Download .fit*, one at a time.
- **Strava export** (fastest for the whole history, no subscription needed): strava.com → *Settings* → *My Account* → *Download or Delete Your Account* → *Request Your Archive*. The `activities/` folder of the archive holds the original files (`.fit.gz` accepted as is). Only files from Zwift are kept; the others are refused.

Offline merge of two files, without Garmin:

```sh
docker compose run --rm zwift-map-merge merge-files watch.fit zwift.fit -o merged.fit
```

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `AUTO_MERGE` | `false` | `true`: merge every detected pair without a click |
| `DELETE_SOURCE` | `true` | delete the Zwift copy on Garmin after merging |
| `COPY_ALTITUDE` | `false` | also copy the Zwift virtual altitude into the points |
| `SCAN_DAYS` | `7` | period scanned |
| `SCAN_INTERVAL_MINUTES` | `30` | time between two scans |
| `MIN_OVERLAP` | `0.5` | minimum overlap (shared time / combined time) |
| `MIN_POSITION_RATIO` | `0.5` | minimum share of watch points that get a position |
| `TIME_TOLERANCE_S` | `2` | maximum gap between a watch point and a Zwift point |
| `SOURCE_NAMES` | `zwift` | how the source is recognized (manufacturer or activity name) |
| `SOURCE_MANUFACTURERS` | `260` | FIT manufacturer required for the source (260 = Zwift) |
| `APP_PASSWORD` | *(empty)* | protects the UI with a password (HTTP Basic) |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | *(empty)* | automatic login at startup (without MFA) |
| `ZWIFT_EMAIL` / `ZWIFT_PASSWORD` | *(empty)* | automatic Zwift login at startup (or connect in the UI) |
| `ZWIFT_AUTO_SYNC` | `true` | fetch new Zwift activities at each scan when Zwift is connected |

## Good to know

- The merged activity gets a **new Garmin ID**. Links to the old one stop working.
- Garmin recomputes the statistics on import. The file is still the watch's, so the activity should count normally for load and Training Status. Check it on a first activity.
- The tool uses the unofficial Garmin Connect API (through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)) and the unofficial Zwift Companion API, which can both change without notice.

## Development

```sh
pip install -e .[dev]
pytest
```

MIT license.

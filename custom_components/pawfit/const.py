# Domain constant for the Pawfit integration
DOMAIN = "pawfit"

BASE_URL = "https://pawfitapi.latsen.com/api/v1/"
USER_AGENT = "Pawfit/3 CFNetwork/1390 Darwin/22.0.0"

# Rate limiting (fork change). Location is fetched on a fixed 60 s schedule,
# and requests for an extra refresh (homeassistant.update_entity etc.) are
# ignored until 60 s have passed. MIN_FETCH_SPACING is a second, hard floor
# on any two fetches, a little under 60 s only so that the scheduler's
# rounding (up to 1 s) never makes a scheduled fetch get skipped.
UPDATE_INTERVAL_SECONDS = 60
MIN_FETCH_SPACING_SECONDS = 55
# Daily activity (steps, calories, active time) changes slowly: fetch it at
# most every 15 minutes, and again straight after midnight.
ACTIVITY_INTERVAL_SECONDS = 15 * 60
# If Pawfit stops answering, keep showing the last known data (entities stay
# available) until this long after the last good fetch.
KEEP_LAST_DATA_SECONDS = 15 * 60
# Give up on a single Pawfit request after this long.
REQUEST_TIMEOUT_SECONDS = 30

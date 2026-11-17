"""Project-wide constants (values only: no Flask, no database, no file access).

Step 1 of the restructuring: these used to sit at the top of app.py.
"""

SERVICES = {"lanes": "Number of lanes", "speed": "Speed", "travel": "Travel time", "length": "Length",
            "freeflow_time": "Free flow time", "freeflow_speed": "Free flow speed", "capacity": "Capacity",
            "volumes": "Traffic volumes"}

PROFILE_JOB_TITLES = (
    "Engineer", "Data Analyst", "Researcher", "Student", "Manager", "Other",
)

PROFILE_PHOTO_MAX_BYTES = 5 * 1024 * 1024

PROFILE_PHOTO_SIZE = (512, 512)

ENGINEERING_SPECIALIZATIONS = (
    "Civil Engineering", "Transportation Engineering", "Traffic Engineering",
    "Highway Engineering", "Structural Engineering", "Other",
)

HIDDEN_SERVICES_BY_DEFAULT = {"capacity"}

DATA_INTERVALS = (5, 15, 30, 60)   # minutes between data points the client can ask for

DEFAULT_DATA_INTERVAL = 15

STATUSES = ["Pending", "Processing", "Completed", "Rejected"]

APP_NAME = "TrafficLens"   # change the app name here (shown in the header, page title and landing page)

# Default explanation of each service. The admin can edit these texts from Admin > Services.
SERVICE_DEFAULTS = {
 "lanes": {"icon": "\U0001F6E3\uFE0F", "desc": "The number of traffic lanes on the link."},
 "speed": {"icon": "\u26A1", "desc": "The average speed of vehicles on the link during each hour you selected."},
 "travel": {"icon": "\u23F1\uFE0F", "desc": "How long it takes to drive the whole link during each hour you selected."},
 "length": {"icon": "\U0001F4CF", "desc": "The length of the link, from its start (A) to its end (B)."},
 "freeflow_time": {"icon": "\U0001F552", "desc": "Travel time at 7:00 AM on the public holiday nearest to your study date in the route's country."},
 "freeflow_speed": {"icon": "\U0001F680", "desc": "Travel speed at 7:00 AM on a past weekly-weekend day in the route's country: the one nearest your study date when Traffic volumes is selected, otherwise the most recent one."},
 "capacity": {"icon": "\U0001F699", "desc": "The maximum number of vehicles that can pass through the link in one hour."},
 "volumes": {"icon": "\U0001F4CA", "desc": "Estimated hourly traffic volume calculated from lanes, actual speed, and weekly-weekend free-flow speed using the traffic model."},
}

# One form per service. kind="static": one value for the whole link. kind="hourly": one value per selected hour.
def _f(kind, label, unit, hint): return {"kind": kind, "label": label, "unit": unit, "hint": hint}

FORMS = {
 "lanes": _f("static", "Number of lanes", "lanes", "Enter the number of lanes of the link."),
 "length": _f("static", "Length", "km", "Enter the length of the link in kilometres."),
 "freeflow_speed": _f("static", "Free flow speed", "km/h", "Use Directions API on the latest weekly weekend date before today at 7:00 AM in the route's country."),
 "freeflow_time": _f("static", "Free flow time", "min", "Use Directions API for the nearest public holiday at 7:00 AM in the route's country."),
 "capacity": _f("static", "Capacity", "veh/h", "Enter the maximum number of vehicles per hour the link can carry."),
 "volumes": _f("hourly", "Estimated traffic volume", "veh/h", "Calculated from the traffic model after administrator review."),
 "speed": _f("hourly", "Average speed", "km/h", "Enter the average speed for each hour the client selected."),
 "travel": _f("hourly", "Travel time", "min", "Enter the travel time of the whole link for each hour the client selected."),
}

MAX_GROUP_LINKS = 10

GROUP_MODES = ("single", "multi_period", "multi_link")

AUDIT_LABELS = {
    "request_saved": "Request saved",
    "status_changed": "Status changed",
    "service_saved": "Service value saved",
    "directions_confirmed": "Directions result approved",
    "volumes_lanes_saved": "Lane count saved",
    "volumes_freeflow_accepted": "Free-flow speed accepted",
    "volumes_retry_requested": "MATLAB retry requested",
    "volumes_approved": "Volumes approved and sent to the client",
    "volumes_run_interrupted": "MATLAB run interrupted by an app restart",
}

MAX_VOLUME_LANES = 12

MAX_VOLUME_PER_LANE = 2400  # veh/h/lane, above typical saturation flow

SHARED_FLOW_SERVICES = ("freeflow_time", "freeflow_speed")

ABANDON_MESSAGE = (
    "Stopped waiting: the Scraper returned no data. The original request is kept in history; "
    "a new request can be prepared and approved."
)

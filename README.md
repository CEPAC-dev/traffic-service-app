# TrafficLens

## Run locally

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    Copy-Item .env.example .env
    python app.py

Open http://127.0.0.1:5000
If you do not activate the virtual environment, start the app with `.\.venv\Scripts\python.exe app.py` so it uses the project's installed dependencies instead of a different system Python.
Default admin: admin@demo.com / admin123. Change this password before use outside a local development environment.
Edit `.env` locally and replace both placeholders. Generate a strong `SECRET_KEY` locally (for example, run `python -c "import secrets; print(secrets.token_hex(32))"`); set `OUTSCRAPER_API_KEY` to your own key. Never paste the API key into chat or commit `.env`. Without a configured `SECRET_KEY`, the app creates an ephemeral one and existing sessions will expire when the process restarts.
Needs an internet connection (map tiles, place search and road routing).

## Outscraper Google Directions

The admin-only **Directions API** page uses Outscraper's documented `GET /google-maps-directions` endpoint from the Flask backend. It sends the API key only in the `X-API-KEY` header. The configured route options follow the [official Google Maps Directions reference](https://docs.outscraper.com/endpoints/google-maps-directions/); asynchronous results are retrieved with the [official request-results endpoint](https://docs.outscraper.com/endpoints/requests-requestid/).

Before execution, review and explicitly approve the saved configuration. CSV files must contain `origin,destination` columns and are validated before they can be added. Automated tests mock all provider requests and never use an actual API key or quota.

The current official directions reference does not provide enough verified pricing rules to calculate a route/time-sweep charge. The page therefore labels cost, currency, and billable units unknown and blocks the ordinary approval action; executing requires the separate **Approve despite unknown cost** action and an explicit acknowledgement. Planned HTTP submissions (one per configured route) are not provider billing units. Actual per-request cost remains unavailable: the [balance](https://docs.outscraper.com/endpoints/profile-balance/) and [invoice](https://docs.outscraper.com/endpoints/invoices/) references expose account-level billing information, not a reliable final cost tied to this integration's individual request ID.

After approval, submissions use Outscraper's async mode. The page records provider request IDs and locally stores responses when they are returned. A background monitor checks known provider IDs every 15 seconds for up to 16 cycles (about four minutes), and resumes polling recoverable IDs after an app restart; this only retrieves status/results and never resubmits billable work. The detail page can also poll no more than once per 15 seconds, up to eight automatic checks per visit; **Refresh provider status** can be used later. A timeout or ambiguous response is marked **Unknown** and is never automatically resubmitted. Completed results remain in `mobility.db` and can be viewed or exported as CSV.

### Manual integration check

This optional procedure sends a real, potentially billable request. First configure your own key in `.env` locally; then sign in as the admin, open **Directions API**, create one low-impact route, review the preview, acknowledge that cost is unknown, and click **Approve despite unknown cost**. Confirm that the request appears in history, open its details, and use status refresh if it is still running. Automated tests do not perform this procedure or contact Outscraper.

## Services (Admin > Services)
Enable or disable the built-in services clients can request, or add a custom service.
Custom services can be recorded once per road link or once per selected hour, with optional units and numeric or text values (for example, LOS grades).
**Free flow time** and **Free flow speed** are enabled by default. Their Directions API requests use 7:00 AM in the route's local time zone on the nearest national public holiday to the study date (ties use the earlier date). The country is identified from the route midpoint using OpenStreetMap Nominatim; holiday dates come from the `holidays` package. The selected holiday and time zone are bound to the approval and recorded in the result page, Excel report, and Directions API result export. If the country or time zone cannot be identified, the request is not prepared.
After the provider job completes, its raw response is retained for admin review and the client report shows the exact date used. An administrator must review and enter the numeric service value; the integration does not infer a speed or travel-time field from undocumented provider response shapes.

## Road-link search and requests
On the study page, search road links by partial text from any network attribute. Results are paginated and can be selected directly on the map.
Admins can reject pending or processing requests from the dashboard; rejected requests remain visible in request history and status filters.
For requests that include **Speed** or **Travel time**, the study page calculates point C along the road beyond B. It uses the straight-line A–B distance as Delta, computes `Ratio = routed A–B length / 1.5 km`, and extends from B by `Ratio × Delta`, increasing the extension as needed until the A–C road distance exceeds 1.5 km. The exact calculated C coordinate is saved with the request. Only admins see C by default; an admin can enable client visibility from the request detail page.
From **Admin > open request > API Directions · Review A → C**, the admin reviews the saved A, C, and study times in a popup. TrafficLens detects the route country and time zone from the route coordinates, sets the API region automatically, and converts the local start/end times using that time zone. Interval is entered and sent in minutes; when empty it defaults to the rounded-up Start/End difference in minutes. The Scraper receives A→C; after its response is complete, TrafficLens reads `road_distance_timing(meters:seconds)` and interpolates the elapsed seconds at the saved A→B road distance. Distance, time, and (when requested) speed are shown to the admin for review and are not saved to the client report until **Confirm & Send Results** is pressed. The same workflow links to the exact Scraper request and raw response.
The result page presents a technical summary immediately below the map, followed by per-service details, with a **Download Excel report** action at the end. The workbook includes request metadata and service values/notes in separate sheets.

Clients and admins can view in-app notifications. To receive browser/device notifications, the user must grant permission on the Notifications page; the browser must support notifications, the site must be served over HTTPS (localhost is supported), and TrafficLens must remain open to poll for new alerts.

## Road network (Admin > Network)
The bundled `Kwait Links.zip` road network is connected automatically on first start when no other network has been configured. It contains 2,117 road links; `NUMLANES`, `V0PRT`, and `CAPPRT` are matched to lanes, free-flow speed, and capacity.
You can still upload a Shapefile (.zip), GeoJSON or GeoPackage of road links in Admin > Network and match its columns (lanes, speed limit, capacity, road name).
Static services the network can answer are filled in automatically when a client sends a request. Free flow time and speed are excluded from that lookup because they now use the holiday-based Directions API workflow:
all answered = Completed, some answered = Processing (client already sees them), none = Pending.

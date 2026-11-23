# Turning Movement Four-Leg E2E Test

**Status: BLOCKED — end-to-end execution not started**  
**Assessment date:** 2026-11-23

## Summary

No request was submitted, no traffic-volume or turning-movement calculation was run, and no success video or results file was produced. The available inputs do not establish verified real traffic speeds or independent turning counts, and the local application pages supplied for the test were not responding.

No application code, equations, database records, or existing project data were changed. No external provider request was sent.

## Preflight findings

| Check | Finding |
|---|---|
| Local app availability | Connections to the supplied local pages at ports 5059 and 5068 were refused. There was no running page to use for a real UI workflow or screen recording. |
| Road network file | `Kwait Links.zip` contains 2,117 GIS road-link features in EPSG:20438. It contains lane/speed-limit and traffic-model attributes, but its `SPEED` field has no populated values. No four-arm candidate was accepted as verified from this file. |
| Volume input workbook | `Inquiries_12-10-2026.xlsx` has four data rows, without link IDs, hours, or source/provenance. These rows were not treated as real measurements. |
| MATLAB | MATLAB R2024a and a 32×11 `Models` matrix are present. The runner requires validated per-hour `R`, `Lanes`, `FFS`, and `Actual Speed` inputs; availability of the software does not establish that the real inputs exist. |
| Eight-link group workflow | The grouped service accepts exactly eight links for one period with Traffic volumes. Link pairs map to East (1–2), North (3–4), West (5–6), and South (7–8); each pair must have one entry and one exit. The request route does not verify that all eight geometries meet at one intersection, so that must be checked independently. |
| Result interpretation | The algorithm estimates movements from entering/leaving volumes and turn ratings. It is not a field counter. Real-world validation requires independent observed counts for each movement. |
| External data | Authorization for potentially billable Outscraper requests was given, but no request was sent. |
| Independent reference counts | Not supplied. The user selected independent observed counts for validation, but no file or data source was provided. |

## Workflow stage status

1. **Selecting Intersection:** Not completed; no intersection was finalized and independently verified.
2. **Selecting 8 Links:** Not completed; no eight directed links were approved as belonging to one intersection.
3. **Submitting Request:** Not performed.
4. **Saving Request:** No request ID exists.
5. **Processing:** No Traffic Volumes jobs were started.
6. **Turn Movement Calculation:** Not performed.
7. **Results Validation:** Not possible without generated results and independent observed turning counts.
8. **Final Output:** No results or recording exist.

## Required to resume

- Provide the independent observed turning-count file (or source), including date/time period, leg directions, and Left/Right/Straight/U-Turn counts.
- Provide real hourly speed measurements for all eight directed links, or confirm that the actual Outscraper source should be used. The existing workbook rows are not sufficient evidence of real measurements.
- Start or authorize an isolated local app instance for the browser workflow. The eventual test should use an isolated database so existing records remain untouched.
- Independently verify the chosen intersection geometry and entry/exit orientation before submitting the eight-link order.

Until these inputs are available, the test must remain **not run**, not passed.

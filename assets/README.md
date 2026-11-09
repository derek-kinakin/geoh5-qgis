# Sample data

`GeoscienceANALYST_demo_data.geoh5` is the Flin Flon (Manitoba, Canada) demo project
that Mira Geoscience distributes with the GEOSCIENCE ANALYST installer, built from
public data. A block model ("Geology model") was added to it for testing. It is used
by the test suite as a real GEOSCIENCE ANALYST file and is not part of the plugin.

It contains:

- Points: `observations`, `surface samples`
- Curves and Surfaces under `Geology` and `Surface Tools`, plus a `DEM` surface
- Grid2D: `airborne data` (rotated 20°, three channels)
- GeoImage: `FlinFlon_geology` (JPEG)
- Drillhole groups: `Drillholes` (121 holes, `geology` intervals) and
  `Drillholes_descriptions` (13 holes, `litho` intervals with text descriptions)
- Block model: `Geology model` (not supported by the plugin)

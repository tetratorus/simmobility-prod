"""Freeze the postcode->node golden case from the upstream's OWN reference results (no SimMobility code run).

Source files (all checked into simmobility-prod, unmodified):
  dev/tools/Build&Validate_SupplyData/Validate/Checking_network_consistency/nodes_new.csv   (node x/y, UTM48N)
  dev/tools/Build&Validate_SupplyData/Build/Node_To_Zone/node_to_zone.csv                  (node_type, bus_terminus_node)
  dev/tools/Build&Validate_SupplyData/Build/Postcode_To_Node/sla_address_transformed.csv   (SLA postcodes, UTM48N)
  dev/tools/Build&Validate_SupplyData/Build/Postcode_To_Node/postcode_to_nereastnode.csv   (upstream reference output)

Fixture = all valid nodes + the SLA postcodes inside a Jurong East bounding box.
Run from the repo root:  python rewire/simmobility.mid_term/golden/make_postcode_fixture.py
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
SUPPLY = ROOT / "dev/tools/Build&Validate_SupplyData"
OUT = Path(__file__).resolve().parent / "postcode_to_node_jurong"
JURONG_BBOX_UTM48N = (350000, 356000, 145000, 150000)  # xmin, xmax, ymin, ymax


def main() -> None:
    nodes = pd.read_csv(SUPPLY / "Validate/Checking_network_consistency/nodes_new.csv")[["id", "x", "y"]]
    zone = pd.read_csv(SUPPLY / "Build/Node_To_Zone/node_to_zone.csv", index_col=0)
    nodes = zone[["node_id", "node_type", "bus_terminus_node"]].merge(
        nodes.rename(columns={"id": "node_id"}), on="node_id").sort_values("node_id")
    pc = pd.read_csv(SUPPLY / "Build/Postcode_To_Node/sla_address_transformed.csv")
    x0, x1, y0, y1 = JURONG_BBOX_UTM48N
    pc = pc[pc.x_coord.between(x0, x1) & pc.y_coord.between(y0, y1)]
    pc = pc[["sla_postcode", "x_coord", "y_coord"]].rename(columns={"x_coord": "x", "y_coord": "y"})
    pc = pc.sort_values("sla_postcode")
    ref = pd.read_csv(SUPPLY / "Build/Postcode_To_Node/postcode_to_nereastnode.csv")
    ref = ref[ref.sla_postcode.isin(pc.sla_postcode)][["sla_postcode", "nearest_node", "nearest_node_dist"]]
    (OUT / "inputs").mkdir(parents=True, exist_ok=True)
    (OUT / "expected").mkdir(parents=True, exist_ok=True)
    nodes.reset_index(drop=True).to_parquet(OUT / "inputs/nodes.parquet", index=False)
    pc.reset_index(drop=True).to_parquet(OUT / "inputs/postcodes.parquet", index=False)
    ref.sort_values("sla_postcode").reset_index(drop=True).to_parquet(OUT / "expected/postcode_node_map.parquet",
                                                                      index=False)
    print(len(nodes), "nodes,", len(pc), "postcodes,", len(ref), "reference rows")


if __name__ == "__main__":
    main()

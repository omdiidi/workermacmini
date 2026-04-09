"""Save scenario results JSON to Supabase scenario mirror tables."""
import argparse
import json
import sys

import supabase_client as db


def write_scenario_to_db(scenario_id, project_id, output):
    """Decompose scenario agent output into scenario mirror tables."""
    items_by_trade = {}
    for li in output.get("line_items", []):
        trade = li.get("trade", "Unknown")
        items_by_trade.setdefault(trade, []).append(li)

    for trade, items in items_by_trade.items():
        # 1. scenario_material_items
        db.delete("scenario_material_items", scenario_id=scenario_id, trade=trade)
        mat_rows = [_to_scenario_material_row(scenario_id, project_id, li) for li in items]
        if mat_rows:
            db.post("scenario_material_items", mat_rows)

        # 2. scenario_material_metadata
        mat_total = sum(float(li.get("extended_cost_expected", 0) or 0) for li in items)
        db.upsert("scenario_material_metadata", {
            "scenario_id": scenario_id,
            "trade": trade,
            "total_cost_expected": mat_total,
            "total_cost_low": sum(float(li.get("extended_cost_low", 0) or 0) for li in items),
            "total_cost_high": sum(float(li.get("extended_cost_high", 0) or 0) for li in items),
            "items_high_confidence": sum(1 for i in items if (i.get("material_confidence") or i.get("confidence")) == "high"),
            "items_medium_confidence": sum(1 for i in items if (i.get("material_confidence") or i.get("confidence")) == "medium"),
            "items_low_confidence": sum(1 for i in items if (i.get("material_confidence") or i.get("confidence")) == "low"),
        }, on_conflict="scenario_id,trade")

        # 3. scenario_labor_items
        db.delete("scenario_labor_items", scenario_id=scenario_id, trade=trade)
        lab_rows = [_to_scenario_labor_row(scenario_id, project_id, li) for li in items]
        if lab_rows:
            db.post("scenario_labor_items", lab_rows)

        # 4. scenario_labor_metadata
        db.upsert("scenario_labor_metadata", {
            "scenario_id": scenario_id,
            "trade": trade,
            "total_cost_expected": sum(float(li.get("cost_expected", 0) or 0) for li in items),
            "total_cost_low": sum(float(li.get("cost_low", 0) or 0) for li in items),
            "total_cost_high": sum(float(li.get("cost_high", 0) or 0) for li in items),
            "total_hours_expected": sum(float(li.get("hours_expected", 0) or 0) for li in items),
        }, on_conflict="scenario_id,trade")

        # 5. scenario_anomaly_flags
        db.delete("scenario_anomaly_flags", scenario_id=scenario_id, trade=trade)
        trade_anomalies = [a for a in output.get("anomalies", []) if a.get("trade") == trade]
        if trade_anomalies:
            anomaly_rows = [
                {**a, "scenario_id": scenario_id, "project_id": project_id}
                for a in trade_anomalies
            ]
            db.post("scenario_anomaly_flags", anomaly_rows)

    # 6. Update scenario status
    db.patch("scenarios", {
        "status": "completed",
        "summary": output.get("summary"),
        "reasoning": output.get("reasoning"),
    }, id=scenario_id)

    return {
        "scenario_id": scenario_id,
        "project_id": project_id,
        "trades": list(items_by_trade.keys()),
        "total_line_items": len(output.get("line_items", [])),
    }


def _to_scenario_material_row(scenario_id, project_id, li):
    return {
        "scenario_id": scenario_id,
        "project_id": project_id,
        "item_id": li.get("item_id"),
        "trade": li.get("trade"),
        "description": li.get("description"),
        "quantity": float(li.get("quantity", 0) or 0),
        "unit": li.get("unit"),
        "unit_cost_low": float(li.get("unit_cost_low", 0) or 0),
        "unit_cost_expected": float(li.get("unit_cost_expected", 0) or 0),
        "unit_cost_high": float(li.get("unit_cost_high", 0) or 0),
        "extended_cost_low": float(li.get("extended_cost_low", 0) or 0),
        "extended_cost_expected": float(li.get("extended_cost_expected", 0) or 0),
        "extended_cost_high": float(li.get("extended_cost_high", 0) or 0),
        "confidence": li.get("material_confidence") or li.get("confidence"),
        "price_sources": li.get("price_sources", []),
        "pricing_method": li.get("pricing_method"),
        "pricing_notes": li.get("pricing_notes"),
        "reasoning": li.get("material_reasoning") or li.get("reasoning"),
        "source_refs": li.get("source_refs", []),
        "model_number": li.get("model_number"),
        "manufacturer": li.get("manufacturer"),
    }


def _to_scenario_labor_row(scenario_id, project_id, li):
    return {
        "scenario_id": scenario_id,
        "project_id": project_id,
        "item_id": li.get("item_id"),
        "trade": li.get("trade"),
        "description": li.get("description"),
        "quantity": float(li.get("quantity", 0) or 0),
        "unit": li.get("unit"),
        "crew": li.get("crew", []),
        "total_labor_hours": float(li.get("total_labor_hours", 0) or 0),
        "blended_hourly_rate": float(li.get("blended_hourly_rate", 0) or 0),
        "labor_cost": float(li.get("labor_cost", 0) or 0),
        "hours_low": float(li.get("hours_low", 0) or 0),
        "hours_expected": float(li.get("hours_expected", 0) or 0),
        "hours_high": float(li.get("hours_high", 0) or 0),
        "cost_low": float(li.get("cost_low", 0) or 0),
        "cost_expected": float(li.get("cost_expected", 0) or 0),
        "cost_high": float(li.get("cost_high", 0) or 0),
        "confidence": li.get("labor_confidence") or li.get("confidence"),
        "reasoning_notes": li.get("labor_reasoning") or li.get("reasoning_notes"),
        "site_adjustments": li.get("site_adjustments", []),
        "economies_of_scale_applied": li.get("economies_of_scale_applied", False),
    }


def main():
    parser = argparse.ArgumentParser(description="Save scenario results to Supabase")
    parser.add_argument("--input", required=True, help="Path to scenario_output.json")
    parser.add_argument("--scenario-id", required=True, help="Scenario ID")
    parser.add_argument("--project-id", required=True, help="Project ID")
    args = parser.parse_args()

    with open(args.input) as f:
        output = json.load(f)

    result = write_scenario_to_db(args.scenario_id, args.project_id, output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

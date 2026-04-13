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

    if not items_by_trade:
        print(f"[save] ERROR: No line_items found in scenario_output.json.", file=sys.stderr)
        print(f"[save] Expected a top-level 'line_items' array. Reformat and retry.", file=sys.stderr)
        sys.exit(1)

    for trade, items in items_by_trade.items():
        # 1. scenario_material_items
        db.delete("scenario_material_items", scenario_id=scenario_id, trade=trade)
        mat_rows = [_to_scenario_material_row(scenario_id, project_id, li) for li in items if li.get("is_material")]
        if mat_rows:
            db.post("scenario_material_items", mat_rows)

        # 2. scenario_material_metadata — trust LLM's extended costs
        mat_items = [li for li in items if li.get("is_material")]
        def _mat_ext(li, field_ext, field_uc):
            val = float(li.get(field_ext, 0) or 0)
            if val > 0:
                return val
            qty = float(li.get("quantity", 0) or 0)
            uc = float(li.get(field_uc, 0) or 0)
            return round(qty * uc, 2)
        mat_total = sum(_mat_ext(li, "extended_cost_expected", "unit_cost_expected") for li in mat_items)
        db.upsert("scenario_material_metadata", {
            "scenario_id": scenario_id,
            "trade": trade,
            "total_cost_expected": mat_total,
            "total_cost_low": sum(_mat_ext(li, "extended_cost_low", "unit_cost_low") for li in mat_items),
            "total_cost_high": sum(_mat_ext(li, "extended_cost_high", "unit_cost_high") for li in mat_items),
            "items_high_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "high"),
            "items_medium_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "medium"),
            "items_low_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "low"),
        }, on_conflict="scenario_id,trade")

        # 3. scenario_labor_items
        db.delete("scenario_labor_items", scenario_id=scenario_id, trade=trade)
        lab_rows = [_to_scenario_labor_row(scenario_id, project_id, li) for li in items if li.get("is_labor")]
        if lab_rows:
            db.post("scenario_labor_items", lab_rows)

        # 4. scenario_labor_metadata — derive from hours * rate
        labor_items = [li for li in items if li.get("is_labor")]
        def _lab_cost(li, field_hrs, field_cost):
            hrs = float(li.get(field_hrs, 0) or 0)
            rate = float(li.get("blended_hourly_rate", 0) or 0)
            return round(hrs * rate, 2) if (hrs > 0 and rate > 0) else float(li.get(field_cost, 0) or 0)
        db.upsert("scenario_labor_metadata", {
            "scenario_id": scenario_id,
            "trade": trade,
            "total_cost_expected": sum(_lab_cost(li, "total_labor_hours", "cost_expected") for li in labor_items),
            "total_cost_low": sum(_lab_cost(li, "hours_low", "cost_low") for li in labor_items),
            "total_cost_high": sum(_lab_cost(li, "hours_high", "cost_high") for li in labor_items),
            "total_hours_low": sum(float(li.get("hours_low", 0) or 0) for li in labor_items),
            "total_hours_expected": sum(float(li.get("hours_expected", 0) or 0) for li in labor_items),
            "total_hours_high": sum(float(li.get("hours_high", 0) or 0) for li in labor_items),
            "items_high_confidence": sum(1 for li in labor_items if (li.get("labor_confidence") or li.get("confidence", "")).lower() == "high"),
            "items_medium_confidence": sum(1 for li in labor_items if (li.get("labor_confidence") or li.get("confidence", "")).lower() == "medium"),
            "items_low_confidence": sum(1 for li in labor_items if (li.get("labor_confidence") or li.get("confidence", "")).lower() == "low"),
        }, on_conflict="scenario_id,trade")

        # 5. scenario_anomaly_flags — map to exact DB columns
        db.delete("scenario_anomaly_flags", scenario_id=scenario_id, trade=trade)
        trade_anomalies = [a for a in output.get("anomalies", []) if a.get("trade") == trade]
        if trade_anomalies:
            mapped_anomalies = [{
                "scenario_id": scenario_id,
                "project_id": project_id,
                "trade": a.get("trade", trade),
                "anomaly_type": a.get("anomaly_type", "noted"),
                "category": a.get("category", ""),
                "description": a.get("description", ""),
                "affected_items": [str(i) for i in a.get("affected_items", [])],
                "cost_impact": float(a.get("cost_impact", 0) or 0),
            } for a in trade_anomalies]
            db.post("scenario_anomaly_flags", mapped_anomalies)

    # 6. Update scenario status
    db.patch("scenarios", {
        "status": "completed",
        "progress": 100,
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
    qty = float(li.get("quantity", 0) or 0)
    uc_low = float(li.get("unit_cost_low", 0) or 0)
    uc_exp = float(li.get("unit_cost_expected", 0) or 0)
    uc_high = float(li.get("unit_cost_high", 0) or 0)

    ext_exp = float(li.get("extended_cost_expected", 0) or 0) or round(qty * uc_exp, 2)
    ext_low = float(li.get("extended_cost_low", 0) or 0) or round(qty * uc_low, 2)
    ext_high = float(li.get("extended_cost_high", 0) or 0) or round(qty * uc_high, 2)

    return {
        "scenario_id": scenario_id,
        "project_id": project_id,
        "item_id": li.get("item_id"),
        "trade": li.get("trade"),
        "description": li.get("description"),
        "quantity": qty,
        "unit": li.get("unit"),
        "unit_cost_low": uc_low,
        "unit_cost_expected": uc_exp,
        "unit_cost_high": uc_high,
        "extended_cost_low": ext_low,
        "extended_cost_expected": ext_exp,
        "extended_cost_high": ext_high,
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
    hours = float(li.get("total_labor_hours", 0) or 0)
    rate = float(li.get("blended_hourly_rate", 0) or 0)
    computed_cost = round(hours * rate, 2)

    hours_low = float(li.get("hours_low", 0) or 0)
    hours_high = float(li.get("hours_high", 0) or 0)
    cost_expected = computed_cost if computed_cost > 0 else float(li.get("cost_expected", 0) or 0)
    cost_low = round(hours_low * rate, 2) if (hours_low > 0 and rate > 0) else float(li.get("cost_low", 0) or 0)
    cost_high = round(hours_high * rate, 2) if (hours_high > 0 and rate > 0) else float(li.get("cost_high", 0) or 0)

    return {
        "scenario_id": scenario_id,
        "project_id": project_id,
        "item_id": li.get("item_id"),
        "trade": li.get("trade"),
        "description": li.get("description"),
        "quantity": float(li.get("quantity", 0) or 0),
        "unit": li.get("unit"),
        "crew": li.get("crew", []),
        "total_labor_hours": hours,
        "blended_hourly_rate": rate,
        "labor_cost": computed_cost if computed_cost > 0 else float(li.get("labor_cost", 0) or 0),
        "hours_low": hours_low,
        "hours_expected": float(li.get("hours_expected", 0) or 0),
        "hours_high": hours_high,
        "cost_low": cost_low,
        "cost_expected": cost_expected,
        "cost_high": cost_high,
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
    if result:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

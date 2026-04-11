"""Save estimation results JSON to Supabase tables."""
import argparse
import json
import sys

import supabase_client as db


def write_estimation_to_db(project_id, output):
    """Decompose agent structured output into DB table inserts."""
    trades_seen = set()

    all_items = output.get("line_items", [])
    if not all_items:
        print(f"[save] WARNING: No line items in estimate output. Marking project as error.")
        db.patch("projects", {
            "status": "error",
            "error_message": "Estimation produced no line items — check estimate_output.json format",
        }, id=project_id)
        return

    items_by_trade = {}
    for li in all_items:
        trade = li.get("trade", "Unknown")
        trades_seen.add(trade)
        items_by_trade.setdefault(trade, []).append(li)

    for trade, items in items_by_trade.items():
        # 1. extraction_items
        db.delete("extraction_items", project_id=project_id, trade=trade)
        extraction_rows = [_to_extraction_row(project_id, li) for li in items]
        if extraction_rows:
            db.post("extraction_items", extraction_rows)

        # 2. extraction_metadata
        db.upsert("extraction_metadata", {
            "project_id": project_id,
            "trade": trade,
            "total_items": len(items),
            "material_items": sum(1 for i in items if i.get("is_material")),
            "labor_items": sum(1 for i in items if i.get("is_labor")),
            "extraction_summary": f"{len(items)} items extracted for {trade}",
            "documents_searched": output.get("documents_searched", 0),
            "pages_searched": output.get("pages_searched", 0),
            "warnings": output.get("warnings", []),
        }, on_conflict="project_id,trade")

        # 3. material_items
        db.delete("material_items", project_id=project_id, trade=trade)
        material_rows = [_to_material_row(project_id, li) for li in items if li.get("is_material")]
        if material_rows:
            db.post("material_items", material_rows)

        # 4. material_metadata
        mat_items = [li for li in items if li.get("is_material")]
        mat_total = sum(float(li.get("extended_cost_expected", 0) or 0) for li in mat_items)
        db.upsert("material_metadata", {
            "project_id": project_id,
            "trade": trade,
            "total_material_cost": mat_total,
            "total_cost_low": sum(float(li.get("extended_cost_low", 0) or 0) for li in mat_items),
            "total_cost_expected": mat_total,
            "total_cost_high": sum(float(li.get("extended_cost_high", 0) or 0) for li in mat_items),
            "items_high_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "high"),
            "items_medium_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "medium"),
            "items_low_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "low"),
        }, on_conflict="project_id,trade")

        # 5. labor_items
        db.delete("labor_items", project_id=project_id, trade=trade)
        labor_rows = [_to_labor_row(project_id, li) for li in items if li.get("is_labor")]
        if labor_rows:
            db.post("labor_items", labor_rows)

        # 6. labor_metadata
        lab_items = [li for li in items if li.get("is_labor")]
        lab_total = sum(float(li.get("cost_expected") if li.get("cost_expected") is not None else li.get("labor_cost", 0) or 0) for li in lab_items)
        db.upsert("labor_metadata", {
            "project_id": project_id,
            "trade": trade,
            "total_labor_cost": lab_total,
            "total_labor_hours": sum(float(li.get("total_labor_hours", 0) or 0) for li in lab_items),
            "total_hours_low": sum(float(li.get("hours_low", 0) or 0) for li in lab_items),
            "total_hours_expected": sum(float(li.get("hours_expected", 0) or 0) for li in lab_items),
            "total_hours_high": sum(float(li.get("hours_high", 0) or 0) for li in lab_items),
            "total_cost_low": sum(float(li.get("cost_low", 0) or 0) for li in lab_items),
            "total_cost_expected": lab_total,
            "total_cost_high": sum(float(li.get("cost_high", 0) or 0) for li in lab_items),
            "bls_area_used": output.get("bls_area_used") or "",
            "bls_wage_data": output.get("bls_wage_rates") or {},
            "items_high_confidence": sum(1 for i in lab_items if (i.get("labor_confidence") or i.get("confidence")) == "high"),
            "items_medium_confidence": sum(1 for i in lab_items if (i.get("labor_confidence") or i.get("confidence")) == "medium"),
            "items_low_confidence": sum(1 for i in lab_items if (i.get("labor_confidence") or i.get("confidence")) == "low"),
        }, on_conflict="project_id,trade")

        # 7. anomaly_flags
        db.delete("anomaly_flags", project_id=project_id, trade=trade)
        trade_anomalies = [a for a in output.get("anomalies", []) if a.get("trade") == trade]
        if trade_anomalies:
            for a in trade_anomalies:
                a["project_id"] = project_id
            db.post("anomaly_flags", trade_anomalies)

    # 8. site_intelligence
    site_intel = output.get("site_intelligence")
    if site_intel:
        site_intel["project_id"] = project_id
        db.upsert("site_intelligence", site_intel, on_conflict="project_id")

    # 9. project_briefs
    brief = output.get("brief_data")
    if brief:
        brief["project_id"] = project_id
        db.upsert("project_briefs", brief, on_conflict="project_id")

    # 10. pipeline_summaries (summary_data is a single JSONB column)
    db.upsert("pipeline_summaries", {
        "project_id": project_id,
        "summary_data": {
            "trades_processed": list(trades_seen),
            "total_line_items": len(output.get("line_items", [])),
            "warnings": output.get("warnings", []),
        },
    }, on_conflict="project_id")

    # 11. Update project total + warnings
    all_items = output.get("line_items", [])
    mat_total = sum(float(li.get("extended_cost_expected", 0) or 0) for li in all_items if li.get("is_material"))
    lab_total = sum(float(li.get("cost_expected") if li.get("cost_expected") is not None else li.get("labor_cost", 0) or 0) for li in all_items if li.get("is_labor"))
    total_estimate = mat_total + lab_total

    update_data = {"total_estimate": total_estimate, "status": "completed"}
    db.patch("projects", update_data, id=project_id)

    return {
        "project_id": project_id,
        "total_estimate": total_estimate,
        "trades": list(trades_seen),
        "total_line_items": len(all_items),
    }


def _to_extraction_row(project_id, li):
    return {
        "project_id": project_id,
        "item_id": li.get("item_id") or "",
        "trade": li.get("trade") or "",
        "description": li.get("description") or "",
        "quantity": float(li.get("quantity", 0) or 0),
        "unit": li.get("unit") or "",
        "spec_reference": li.get("spec_reference"),
        "model_number": li.get("model_number"),
        "manufacturer": li.get("manufacturer"),
        "material_description": li.get("material_description"),
        "notes": li.get("notes"),
        "work_action": li.get("work_action"),
        "line_item_type": li.get("line_item_type"),
        "bid_group": li.get("bid_group"),
        "source_refs": li.get("source_refs", []),
        "is_material": bool(li.get("is_material", False)),
        "is_labor": bool(li.get("is_labor", False)),
        "extraction_confidence": li.get("extraction_confidence") or "medium",
    }


def _to_material_row(project_id, li):
    return {
        "project_id": project_id,
        "item_id": li.get("item_id") or "",
        "trade": li.get("trade") or "",
        "description": li.get("description") or "",
        "quantity": float(li.get("quantity", 0) or 0),
        "unit": li.get("unit") or "",
        "unit_cost_low": float(li.get("unit_cost_low", 0) or 0),
        "unit_cost_expected": float(li.get("unit_cost_expected", 0) or 0),
        "unit_cost_high": float(li.get("unit_cost_high", 0) or 0),
        "extended_cost_low": float(li.get("extended_cost_low", 0) or 0),
        "extended_cost_expected": float(li.get("extended_cost_expected", 0) or 0),
        "extended_cost_high": float(li.get("extended_cost_high", 0) or 0),
        "confidence": li.get("material_confidence") or li.get("confidence") or "medium",
        "price_sources": li.get("price_sources", []),
        "pricing_method": li.get("pricing_method") or "",
        "pricing_notes": li.get("pricing_notes"),
        "reasoning": li.get("material_reasoning") or li.get("reasoning"),
        "source_refs": li.get("source_refs", []),
        "model_number": li.get("model_number"),
        "manufacturer": li.get("manufacturer"),
        "material_description": li.get("material_description", ""),
    }


def _to_labor_row(project_id, li):
    return {
        "project_id": project_id,
        "item_id": li.get("item_id") or "",
        "trade": li.get("trade") or "",
        "description": li.get("description") or "",
        "quantity": float(li.get("quantity", 0) or 0),
        "unit": li.get("unit") or "",
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
        "confidence": li.get("labor_confidence") or li.get("confidence") or "medium",
        "reasoning_notes": li.get("labor_reasoning") or li.get("reasoning_notes") or "",
        "site_adjustments": li.get("site_adjustments", []),
        "source_refs": li.get("source_refs", []),
        "economies_of_scale_applied": bool(li.get("economies_of_scale_applied", False)),
        "base_hours": float(li.get("base_hours", 0) or 0),
        "adjusted_hours": float(li.get("adjusted_hours", 0) or 0),
        "productivity_rate": li.get("productivity_rate"),
    }


def main():
    parser = argparse.ArgumentParser(description="Save estimation results to Supabase")
    parser.add_argument("--input", required=True, help="Path to estimate_output.json")
    parser.add_argument("--project-id", required=True, help="Project ID")
    args = parser.parse_args()

    with open(args.input) as f:
        output = json.load(f)

    result = write_estimation_to_db(args.project_id, output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

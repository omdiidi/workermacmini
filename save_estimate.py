"""Save estimation results JSON to Supabase tables."""
import argparse
import json
import sys
from datetime import datetime, timezone

import supabase_client as db


def _safe_list(val):
    """Ensure val is a list of dicts. LLM sometimes outputs strings instead."""
    if not isinstance(val, list):
        return []
    return [x for x in val if isinstance(x, dict)]


def write_estimation_to_db(project_id, output):
    """Decompose agent structured output into DB table inserts."""
    try:
        return _write_estimation_to_db_inner(project_id, output)
    except Exception as e:
        try:
            db.patch("projects", {
                "status": "error",
                "error_message": f"Save failed: {str(e)[:500]}",
            }, id=project_id)
        except Exception:
            pass
        raise


def _write_estimation_to_db_inner(project_id, output):
    trades_seen = set()

    all_items = _safe_list(output.get("line_items", []))
    if not all_items:
        raise RuntimeError(
            "No line_items array found in estimate_output.json. "
            "Expected a top-level 'line_items' array with objects containing "
            "'is_material', 'is_labor', 'trade', 'description', 'quantity', etc. "
            "Reformat estimate_output.json to match this schema and run save-to-db again."
        )

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

        # 4. material_metadata — trust LLM's extended costs (may include volume discounts)
        mat_items = [li for li in items if li.get("is_material")]
        def _mat_ext(li, field_ext, field_uc):
            raw = li.get(field_ext)
            if raw is not None and raw != "":
                return float(raw)
            qty = float(li.get("quantity", 0) or 0)
            uc = float(li.get(field_uc, 0) or 0)
            return round(qty * uc, 2)
        mat_total = sum(_mat_ext(li, "extended_cost_expected", "unit_cost_expected") for li in mat_items)
        db.upsert("material_metadata", {
            "project_id": project_id,
            "trade": trade,
            "total_material_cost": mat_total,
            "total_cost_low": sum(_mat_ext(li, "extended_cost_low", "unit_cost_low") for li in mat_items),
            "total_cost_expected": mat_total,
            "total_cost_high": sum(_mat_ext(li, "extended_cost_high", "unit_cost_high") for li in mat_items),
            "items_high_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "high"),
            "items_medium_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "medium"),
            "items_low_confidence": sum(1 for i in mat_items if (i.get("material_confidence") or i.get("confidence")) == "low"),
        }, on_conflict="project_id,trade")

        # 5. labor_items
        db.delete("labor_items", project_id=project_id, trade=trade)
        labor_rows = [_to_labor_row(project_id, li) for li in items if li.get("is_labor")]
        if labor_rows:
            db.post("labor_items", labor_rows)

        # 6. labor_metadata — derive from hours * rate
        lab_items = [li for li in items if li.get("is_labor")]
        def _lab_cost(li, field_hrs, field_cost):
            hrs = float(li.get(field_hrs, 0) or 0)
            rate = float(li.get("blended_hourly_rate", 0) or 0)
            return round(hrs * rate, 2) if (hrs > 0 and rate > 0) else float(li.get(field_cost, 0) or 0)
        lab_total = sum(_lab_cost(li, "total_labor_hours", "cost_expected") for li in lab_items)
        db.upsert("labor_metadata", {
            "project_id": project_id,
            "trade": trade,
            "total_labor_cost": lab_total,
            "total_labor_hours": sum(float(li.get("total_labor_hours", 0) or 0) for li in lab_items),
            "total_hours_low": sum(float(li.get("hours_low", 0) or 0) for li in lab_items),
            "total_hours_expected": sum(float(li.get("hours_expected", 0) or 0) for li in lab_items),
            "total_hours_high": sum(float(li.get("hours_high", 0) or 0) for li in lab_items),
            "total_cost_low": sum(_lab_cost(li, "hours_low", "cost_low") for li in lab_items),
            "total_cost_expected": lab_total,
            "total_cost_high": sum(_lab_cost(li, "hours_high", "cost_high") for li in lab_items),
            "bls_area_used": output.get("bls_area_used") or "",
            "bls_wage_data": output.get("bls_wage_rates") or {},
            "items_high_confidence": sum(1 for i in lab_items if (i.get("labor_confidence") or i.get("confidence")) == "high"),
            "items_medium_confidence": sum(1 for i in lab_items if (i.get("labor_confidence") or i.get("confidence")) == "medium"),
            "items_low_confidence": sum(1 for i in lab_items if (i.get("labor_confidence") or i.get("confidence")) == "low"),
        }, on_conflict="project_id,trade")

        # 7. anomaly_flags — map to exact DB columns
        db.delete("anomaly_flags", project_id=project_id, trade=trade)
        trade_anomalies = [a for a in _safe_list(output.get("anomalies", [])) if a.get("trade") == trade]
        if trade_anomalies:
            mapped_anomalies = [{
                "project_id": project_id,
                "trade": a.get("trade", trade),
                "anomaly_type": a.get("anomaly_type", "noted"),
                "category": a.get("category", ""),
                "description": a.get("description", ""),
                "affected_items": [str(i) for i in (a.get("affected_items") if isinstance(a.get("affected_items"), list) else [])],
                "cost_impact": float(a.get("cost_impact", 0) or 0),
            } for a in trade_anomalies]
            db.post("anomaly_flags", mapped_anomalies)

    # 8. site_intelligence — wrap in expected JSONB columns
    site_intel = output.get("site_intelligence")
    if isinstance(site_intel, str):
        site_intel = {"project_findings": site_intel}
    if isinstance(site_intel, dict):
        db.upsert("site_intelligence", {
            "project_id": project_id,
            "item_annotations": site_intel.get("item_annotations", {}),
            "project_findings": site_intel.get("project_findings", {}),
            "procurement_intel": site_intel.get("procurement_intel", {}),
            "estimation_guidance": site_intel.get("estimation_guidance", {}),
        }, on_conflict="project_id")

    # 9. project_briefs — wrap in expected columns
    brief = output.get("brief_data")
    if isinstance(brief, str):
        brief = {"key_findings": brief}
    if isinstance(brief, dict):
        db.upsert("project_briefs", {
            "project_id": project_id,
            "project_classification": brief.get("project_classification", ""),
            "facility_description": brief.get("facility_description", ""),
            "key_findings": brief.get("key_findings", ""),
            "scope_summary": brief.get("scope_summary", ""),
            "document_summary": brief.get("document_summary", ""),
            "extraction_focus": brief.get("extraction_focus", ""),
            "generation_notes": brief.get("generation_notes", ""),
            "brief_data": brief,  # also store the full blob
        }, on_conflict="project_id")

    # 10. pipeline_summaries (summary_data is a single JSONB column)
    db.upsert("pipeline_summaries", {
        "project_id": project_id,
        "summary_data": {
            "trades_processed": list(trades_seen),
            "total_line_items": len(all_items),
            "warnings": output.get("warnings", []),
        },
    }, on_conflict="project_id")

    # 11. Update project total + warnings
    # Materials: trust LLM's extended_cost (may reflect volume discounts)
    # Labor: always derive from hours * rate (prevents material-cost duplication bug)
    mat_total = 0.0
    for li in all_items:
        if li.get("is_material"):
            raw = li.get("extended_cost_expected")
            if raw is not None and raw != "":
                mat_total += float(raw)
            else:
                qty = float(li.get("quantity", 0) or 0)
                uc = float(li.get("unit_cost_expected", 0) or 0)
                mat_total += round(qty * uc, 2)
    lab_total = 0.0
    for li in all_items:
        if li.get("is_labor"):
            hrs = float(li.get("total_labor_hours", 0) or 0)
            rate = float(li.get("blended_hourly_rate", 0) or 0)
            if hrs > 0 and rate > 0:
                lab_total += round(hrs * rate, 2)
            else:
                lab_total += float(li.get("cost_expected") if li.get("cost_expected") is not None else li.get("labor_cost", 0) or 0)
    total_estimate = mat_total + lab_total

    update_data = {
        "total_estimate": total_estimate,
        "status": "completed",
        "stage": "completed",
        "progress": 100,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "error_message": None,
    }
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
    qty = float(li.get("quantity", 0) or 0)
    uc_low = float(li.get("unit_cost_low", 0) or 0)
    uc_exp = float(li.get("unit_cost_expected", 0) or 0)
    uc_high = float(li.get("unit_cost_high", 0) or 0)

    # Use the LLM's extended_cost if provided — it may reflect volume discounts,
    # negotiated pricing, or package deals that differ from simple qty * unit_cost.
    # Only fall back to multiplication when extended_cost is missing.
    _raw = li.get("extended_cost_expected")
    ext_exp = float(_raw) if _raw is not None and _raw != "" else round(qty * uc_exp, 2)
    _raw = li.get("extended_cost_low")
    ext_low = float(_raw) if _raw is not None and _raw != "" else round(qty * uc_low, 2)
    _raw = li.get("extended_cost_high")
    ext_high = float(_raw) if _raw is not None and _raw != "" else round(qty * uc_high, 2)

    return {
        "project_id": project_id,
        "item_id": li.get("item_id") or "",
        "trade": li.get("trade") or "",
        "description": li.get("description") or "",
        "quantity": qty,
        "unit": li.get("unit") or "",
        "unit_cost_low": uc_low,
        "unit_cost_expected": uc_exp,
        "unit_cost_high": uc_high,
        "extended_cost_low": ext_low,
        "extended_cost_expected": ext_exp,
        "extended_cost_high": ext_high,
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
    hours = float(li.get("total_labor_hours", 0) or 0)
    rate = float(li.get("blended_hourly_rate", 0) or 0)
    computed_cost = round(hours * rate, 2)

    # Trust hours and rate as the LLM's labor judgment; derive costs from them.
    # Prevents a known issue where cost_expected gets copied from material costs.
    hours_low = float(li.get("hours_low", 0) or 0)
    hours_high = float(li.get("hours_high", 0) or 0)
    cost_expected = computed_cost if computed_cost > 0 else float(li.get("cost_expected", 0) or 0)
    cost_low = round(hours_low * rate, 2) if (hours_low > 0 and rate > 0) else float(li.get("cost_low", 0) or 0)
    cost_high = round(hours_high * rate, 2) if (hours_high > 0 and rate > 0) else float(li.get("cost_high", 0) or 0)

    return {
        "project_id": project_id,
        "item_id": li.get("item_id") or "",
        "trade": li.get("trade") or "",
        "description": li.get("description") or "",
        "quantity": float(li.get("quantity", 0) or 0),
        "unit": li.get("unit") or "",
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
    if result:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

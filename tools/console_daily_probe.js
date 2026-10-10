/*
 * The DOM probe for the daily-operations click-through (tools/console_daily_clickthrough.sh).
 *
 * It returns ONE object holding every counter the harness reports. Everything it reads is
 * text or an attribute the console already renders from the engine's payload - the probe
 * computes no state of its own, so a counter it prints is a number that was on the screen.
 *
 * `stat(label)` finds a <Stat> card by its label: the component renders exactly
 * <div><div>{label}</div><div>{value}</div></div>, which is what the shape test matches.
 * `text(sel)` collapses whitespace so a multi-line panel reads as one line in the log.
 *
 * Run it through the harness, not by hand:
 *   agent-browser --session browser eval "$(cat tools/console_daily_probe.js)"
 */
(() => {
  const stat = (label) => {
    for (const box of document.querySelectorAll("div")) {
      if (box.children.length !== 2) continue;
      const head = box.children[0];
      const value = box.children[1];
      if (head.tagName !== "DIV" || head.children.length !== 0) continue;
      if (value.children.length !== 0) continue;
      if (head.textContent.trim() !== label) continue;
      return value.textContent.trim();
    }
    return null;
  };
  const flat = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : null);
  const text = (sel) => flat(document.querySelector(sel));
  const texts = (sel) => Array.from(document.querySelectorAll(sel)).map(flat);
  const tokens = (sel) =>
    Array.from(document.querySelectorAll(sel + " [data-token]")).map((t) => t.getAttribute("data-token"));
  const attr = (sel, name) => {
    const el = document.querySelector(sel);
    return el ? el.getAttribute(name) : null;
  };
  const button = (sel, name) => {
    const el = document.querySelector(sel);
    return el ? el[name] : null;
  };

  return {
    // --- readiness ------------------------------------------------------
    hydrated: document.body.textContent.includes("BROWSER_HASH_MATCHES_ENGINE"),
    bridge_error_banners: document.querySelectorAll("[data-testid=bridge-error-banner]").length,
    refusal_banners: texts("[data-testid=refusal-banner]"),

    // --- 1. the live queue ----------------------------------------------
    queue_row_count: document.querySelectorAll("[data-testid=queue-row]").length,
    review_row_count: document.querySelectorAll("[data-testid=review-row]").length,
    queue_rows: Array.from(document.querySelectorAll("[data-testid=queue-row]")).map((r) => ({
      id: r.getAttribute("data-debtor-id"),
      status: r.getAttribute("data-status"),
      shifted: r.getAttribute("data-shifted"),
      status_cell: flat(r.children[1]),
      schedule_cell: r.children[5] ? flat(r.children[5]) : null,
    })),
    stat_debtors: stat("Debtors"),
    stat_pending_review: stat("Pending review"),
    stat_scheduled_runs: stat("Scheduled runs"),
    stat_dnc: stat("DNC records"),
    shift_note: text("[data-testid=shift-note]"),

    // --- 2. mode, the halt, the schedule --------------------------------
    mode_in_force: tokens("[data-testid=mode-panel]").slice(0, 1),
    hitl_banner: text("[data-testid=hitl-banner]"),
    full_auto_selectable: attr("[data-testid=mode-full-auto]", "data-selectable"),
    full_auto_disabled: button("[data-testid=mode-full-auto]", "disabled"),
    fullauto_locked_reason: text("[data-testid=fullauto-locked]"),
    schedule_header: (() => {
      const panel = document.querySelector("[data-testid=mode-panel]");
      const match = (panel ? panel.textContent : "").match(/The client's schedule \((\d+) runs?\)/);
      return match ? match[1] : null;
    })(),
    scheduled_runs: texts("[data-testid=scheduled-runs] li"),
    halt_result: text("[data-testid=halt-result]"),
    timeline_rows: texts("[data-testid=timeline-row]"),

    // --- 3. the human gate ----------------------------------------------
    stat_pending_drafts: stat("Pending drafts"),
    stat_hitl_reviews: stat("Hitl reviews counted"),
    stat_guardrail_blocks: stat("Guardrail blocks"),
    review_rows: Array.from(document.querySelectorAll("[data-testid=review-row]")).map((r) => ({
      id: r.getAttribute("data-debtor-id"),
      text: flat(r).slice(0, 220),
    })),
    review_gate_tokens: tokens("[data-testid=review-grid]"),
    dispatch_result: text("[data-testid=dispatch-result]"),
    dispatch_lines: texts("[data-testid=dispatch-line]"),
    milestone_detail: text("[data-testid=milestone-detail]"),

    // --- 4. the interaction-history drawer ------------------------------
    history_drawer_open: document.querySelector("[data-testid=history-drawer]") !== null,
    history_header: (() => {
      const drawer = document.querySelector("[data-testid=history-drawer]");
      return drawer ? flat(drawer).slice(0, 220) : null;
    })(),
    history_entries: document.querySelectorAll("[data-testid=history-entry]").length,

    // --- 5. the alert feed ----------------------------------------------
    alerts_total: document.querySelectorAll("[data-testid=alert-entry]").length,
    alerts_high: document.querySelectorAll("[data-testid=alert-entry][data-severity=high]").length,
    alert_sample: texts("[data-testid=alert-entry]").slice(0, 3),

    // --- 6. the money trail ---------------------------------------------
    stat_collected: stat("Collected to date"),
    stat_fee: stat("Our 20% fee"),
    stat_ledger_lines: stat("Ledger lines"),
    stat_statement: stat("Statement"),
    collected_label: text("[data-testid=collected-label]"),
    simulation_result: text("[data-testid=simulation-result]"),
    ledger_rows: texts("[data-testid=ledger-row]"),
    statement_card: text("[data-testid=statement-card]"),
  };
})()

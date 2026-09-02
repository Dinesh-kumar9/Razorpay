/**
 * Project Meridian Dashboard — Interactive Demo Logic
 *
 * Handles:
 *   - applyPreset(): sets form fields to known guardrail-triggering scenarios
 *   - runDemo(): submits a transaction through the full pipeline API
 *   - renderResult(): renders the AuditRecord response into the demo result area
 *
 * Guardrail rule reference (must match policy_engine/rules.py):
 *   HARD_STOP_001: failure_code in {card_blocked, fraud_flag, kyc_hold, stolen_card}
 *   HARD_STOP_002: failure_code in {card_expired, invalid_card} + retry action
 *   RATE_LIMIT_001: retry_count_so_far >= MAX_RETRIES (3)
 *   CONTACT_LIMIT_002: customer_contact_count_24h >= 1
 *   COOLDOWN_001: last_contact_time within 30 minutes
 *   WINDOW_001: time_of_failure outside 09:00–21:00 (TRAI DND: 21:00–09:00 IST)
 */

"use strict";

/**
 * Apply a guardrail preset to the demo form fields.
 * @param {string} name - One of: hard_stop | rate_limit | cooldown | dnd_window | clean
 */
function applyPreset(name) {
  var presets = {
    hard_stop: {
      failure: "fraud_flag",
      amount: 50000,
      method: "card",
      retryCount: 0,
      contactMin: null,
      hour: 14,
      hint: "HARD_STOP_001 will fire — fraud_flag always → escalate_to_human regardless of model.",
    },
    rate_limit: {
      failure: "insufficient_funds",
      amount: 8000,
      method: "upi",
      retryCount: 3,
      contactCount: 1,
      contactMin: null,
      hour: 14,
      hint: "RATE_LIMIT_001 will fire (retry_count=3 ≥ MAX_RETRIES_PER_TXN). RATE_LIMIT_002 also fires (customer_contact_count_24h=1 ≥ 1).",
    },
    cooldown: {
      failure: "network_timeout",
      amount: 15000,
      method: "card",
      retryCount: 1,
      contactCount: 0,
      contactMin: 15,
      hour: 14,
      hint: "COOLDOWN_001 will fire — contacted 15 min ago, within the 30-min cooldown window.",
    },
    dnd_window: {
      failure: "gateway_error",
      amount: 5000,
      method: "netbanking",
      retryCount: 0,
      contactCount: 0,
      contactMin: null,
      hour: 22,
      hint: "WINDOW_001 will fire — 22:00 is inside the TRAI DND window (21:00–09:00 IST).",
    },
    clean: {
      failure: "card_expired",
      amount: 25000,
      method: "card",
      retryCount: 0,
      contactCount: 0,
      contactMin: null,
      hour: 14,
      hint: "No guardrail fires — card_expired with 0 retries, model chooses nudge_alt_method.",
    },
  };

  var p = presets[name];
  if (!p) return;

  setFieldValue("demo-failure", p.failure);
  setFieldValue("demo-amount", p.amount);
  setFieldValue("demo-method", p.method);
  setFieldValue("demo-retry-count", p.retryCount);
  setFieldValue("demo-contact-min", p.contactMin !== null ? p.contactMin : "");
  setFieldValue("demo-hour", p.hour !== null ? p.hour : "");
  // store contactCount on form element for runDemo to pick up
  var form = document.getElementById("demo-form");
  if (form) form.dataset.contactCount = p.contactCount !== undefined ? p.contactCount : 0;

  // Show hint strip
  var hint = document.getElementById("demo-preset-hint");
  if (!hint) {
    hint = document.createElement("div");
    hint.id = "demo-preset-hint";
    hint.className = "preset-hint";
    var form = document.getElementById("demo-form");
    form.parentNode.insertBefore(hint, form);
  }
  hint.textContent = "⬆ " + p.hint;
  hint.style.display = "block";

  // Highlight active preset button
  document.querySelectorAll(".preset-btn").forEach(function (b) {
    b.classList.remove("preset-btn--active");
  });
  var clicked = document.querySelector('[onclick="applyPreset(\'' + name + '\')"]');
  if (clicked) clicked.classList.add("preset-btn--active");
}

function setFieldValue(id, value) {
  var el = document.getElementById(id);
  if (el) el.value = value;
}

/**
 * Build an ISO-8601 timestamp for a given hour of day (today's date).
 * If hour is not provided, uses the current time.
 */
function buildTimestamp(hour) {
  var now = new Date();
  if (hour !== null && hour !== "" && !isNaN(parseInt(hour, 10))) {
    now.setHours(parseInt(hour, 10), 30, 0, 0);
  }
  return now.toISOString();
}

/**
 * Build an ISO-8601 timestamp for last_contact_time, given minutes ago.
 * Returns null if contactMin is empty/null.
 */
function buildLastContactTime(contactMin) {
  if (contactMin === "" || contactMin === null) return null;
  var min = parseInt(contactMin, 10);
  if (isNaN(min)) return null;
  var t = new Date(Date.now() - min * 60 * 1000);
  return t.toISOString();
}

/**
 * Run a single transaction through the full agent pipeline via the API.
 * Reads all demo form fields, including guardrail-triggering fields.
 */
async function runDemo() {
  var btn = document.getElementById("demo-run-btn");
  var result = document.getElementById("demo-result");

  var amount = parseFloat(document.getElementById("demo-amount").value) || 25000;
  var failure = document.getElementById("demo-failure").value;
  var method = document.getElementById("demo-method").value;
  var retryCount = parseInt(document.getElementById("demo-retry-count").value, 10) || 0;
  var contactMin = document.getElementById("demo-contact-min").value;
  var hourVal = document.getElementById("demo-hour").value;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Running pipeline&hellip;';
  result.className = "demo-result demo-result--loading";
  result.innerHTML =
    '<div class="pipeline-steps">' +
    '<span class="ps ps--done">&#x2713; Ingestion</span>' +
    '<span class="ps ps--active">&#x21BB; Risk Model</span>' +
    '<span class="ps ps--wait">&#x25CB; Policy Engine</span>' +
    '<span class="ps ps--wait">&#x25CB; LLM Layer</span>' +
    '<span class="ps ps--wait">&#x25CB; Audit</span>' +
    "</div>";

  var txnId = "DEMO-" + Date.now();
  var lastContact = buildLastContactTime(contactMin);
  var timeOfFailure = buildTimestamp(hourVal);

  var form = document.getElementById("demo-form");
  var contactCount = form && form.dataset.contactCount !== undefined
    ? parseInt(form.dataset.contactCount, 10) || 0
    : (retryCount > 0 ? retryCount : 0);

  var payload = {
    txn_id: txnId,
    amount_inr: amount.toFixed(2),
    failure_code: failure,
    payment_method: method,
    customer_id: "cust_demo_001",
    merchant_id: "merch_demo_001",
    time_of_failure: timeOfFailure,
    retry_count_so_far: retryCount,
    customer_contact_count_24h: contactCount,
    last_contact_time: lastContact,
    gateway_raw_error: failure.replace(/_/g, " "),
    is_subscription: false,
  };

  var upd = function (doneIdx, nextActiveIdx) {
    var steps = document.querySelectorAll(".ps");
    if (steps[doneIdx]) {
      steps[doneIdx].className = "ps ps--done";
      steps[doneIdx].innerHTML =
        "&#x2713; " + steps[doneIdx].textContent.replace(/^[✓⟳◯] /, "");
    }
    if (nextActiveIdx >= 0 && steps[nextActiveIdx]) {
      steps[nextActiveIdx].className = "ps ps--active";
    }
  };

  setTimeout(function () { upd(1, 2); }, 400);
  setTimeout(function () { upd(2, 3); }, 800);
  setTimeout(function () { upd(3, 4); }, 1100);

  try {
    var resp = await fetch("/api/simulate/single", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(await resp.text());
    var rec = await resp.json();
    setTimeout(function () {
      upd(4, -1);
      renderResult(result, rec);
      btn.disabled = false;
      btn.innerHTML = '<span class="btn-icon">&#x25B6;</span> Run Again';
    }, 1400);
  } catch (e) {
    result.className = "demo-result demo-result--error";
    result.innerHTML = "<strong>Error:</strong> " + e.message;
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-icon">&#x25B6;</span> Run Agent';
  }
}

/**
 * Render the AuditRecord API response into the demo result panel.
 * Highlights guardrail overrides prominently — this is the pitch moment.
 */
function renderResult(el, rec) {
  var recovered = rec.simulated_outcome && rec.simulated_outcome.recovered;
  var amt = parseFloat(
    (rec.simulated_outcome && rec.simulated_outcome.amount_recovered_inr) || 0
  );
  var action = rec.final_action || "";
  var cm = {
    retry_now: "action-badge--retry-now",
    retry_delayed: "action-badge--retry-delayed",
    stop: "action-badge--stop",
    escalate_to_human: "action-badge--escalate-to-human",
    nudge_alt_method: "action-badge--nudge-alt-method",
  };

  var overrideBanner = "";
  if (rec.was_overridden) {
    overrideBanner =
      '<div class="demo-override-pill demo-override-pill--prominent">' +
      "&#x1F6E1;&#xFE0F; <strong>Guardrail fired:</strong> <code>" +
      rec.guardrail_rule_id +
      "</code>" +
      '<div class="override-reason">' + (rec.override_reason || "") + "</div>" +
      "</div>";
  }

  el.className = "demo-result demo-result--show";
  el.innerHTML =
    '<div class="demo-result-grid">' +
    '<div class="demo-result-main">' +
    '<div class="demo-result-label">Final Action</div>' +
    '<span class="action-badge action-badge--lg ' + (cm[action] || "") + '">' +
    action.replace(/_/g, " ").toUpperCase() +
    "</span>" +
    overrideBanner +
    '<div class="demo-outcome ' + (recovered ? "outcome-recovered" : "outcome-failed") + '">' +
    (recovered
      ? "&#x2705; Recovered &#x20B9;" +
        amt.toLocaleString("en-IN", { maximumFractionDigits: 0 })
      : "&#x274C; Not recovered this attempt") +
    "</div></div>" +
    '<div class="demo-result-explanation">' +
    '<div class="demo-result-label">Agent Explanation</div>' +
    '<div class="demo-explanation-text">' +
    (rec.explanation && rec.explanation.rationale ? rec.explanation.rationale : "&mdash;") +
    "</div>" +
    '<div class="demo-caveat">' +
    (rec.explanation && rec.explanation.confidence_caveat ? rec.explanation.confidence_caveat : "") +
    "</div></div></div>" +
    '<a href="/transaction/' +
    rec.txn_id +
    '" class="btn btn--outline btn--sm" target="_blank">View Full Audit Record &#x2197;</a>';
}

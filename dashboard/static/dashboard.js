/**
 * Project Meridian — Case-File Ledger Dashboard Client
 * Renders decisions in strict states: PROPOSED, OVERRIDDEN, MANDATED, CONFIRMED.
 * Every decision element carries data-action, data-rule-id, and data-source attributes.
 */

var PRESETS = {
  cooldown: {
    failure: "network_timeout",
    amount: 5000,
    method: "upi",
    retryCount: 1,
    lastContact: 15,
    hour: 16,
    contactCount: 0,
    desc: "COOLDOWN_001 — Genuine Override: Model proposed RETRY_NOW, but contact 15m ago forces RETRY_DELAYED",
  },
  hard_stop: {
    failure: "fraud_flag",
    amount: 50000,
    method: "card",
    retryCount: 0,
    lastContact: "",
    hour: 14,
    contactCount: 0,
    desc: "HARD_STOP_001 — RBI fraud flag: Mandates ESCALATE_TO_HUMAN (concurs with model's safety prediction)",
  },
  rate_limit: {
    failure: "insufficient_funds",
    amount: 15000,
    method: "card",
    retryCount: 3,
    lastContact: 120,
    hour: 15,
    contactCount: 1,
    desc: "RATE_LIMIT_001 — Genuine Override: Model proposed RETRY_DELAYED, but retry_count >= 3 forces STOP",
  },
  dnd_window: {
    failure: "insufficient_funds",
    amount: 25000,
    method: "card",
    retryCount: 0,
    lastContact: "",
    hour: 22,
    contactCount: 0,
    desc: "WINDOW_001 — Genuine Override: Model proposed immediate contact, 22:00 IST quiet hours force RETRY_DELAYED",
  },
  clean: {
    failure: "card_expired",
    amount: 25000,
    method: "card",
    retryCount: 0,
    lastContact: "",
    hour: 14,
    contactCount: 0,
    desc: "Clean transaction — Instrument expired, nudge alternative method cleanly",
  },
};

function applyPreset(name) {
  var p = PRESETS[name];
  if (!p) return;

  var fAmount = document.getElementById("demo-amount");
  var fFailure = document.getElementById("demo-failure");
  var fMethod = document.getElementById("demo-method");
  var fRetry = document.getElementById("demo-retry-count");
  var fContact = document.getElementById("demo-contact-min");
  var fHour = document.getElementById("demo-hour");
  var form = document.getElementById("demo-form");

  if (fAmount) fAmount.value = p.amount;
  if (fFailure) fFailure.value = p.failure;
  if (fMethod) fMethod.value = p.method;
  if (fRetry) fRetry.value = p.retryCount;
  if (fContact) fContact.value = p.lastContact;
  if (fHour) fHour.value = p.hour;
  if (form) form.dataset.contactCount = p.contactCount;

  document.querySelectorAll(".preset-button").forEach(function (btn) {
    btn.classList.remove("preset-button--active");
  });
  var activeBtn = document.getElementById("btn-preset-" + name);
  if (activeBtn) activeBtn.classList.add("preset-button--active");

  var hint = document.getElementById("preset-hint");
  if (hint) {
    hint.textContent = p.desc;
  }
}

async function runDemo() {
  var btn = document.getElementById("demo-run-btn");
  var result = document.getElementById("demo-result");
  if (!btn || !result) return;

  btn.disabled = true;
  btn.innerHTML = '<span class="data-value">Evaluating...</span>';

  var amount = parseFloat(document.getElementById("demo-amount").value) || 25000;
  var failure = document.getElementById("demo-failure").value;
  var method = document.getElementById("demo-method").value;
  var retryCount = parseInt(document.getElementById("demo-retry-count").value, 10) || 0;
  var lastContactMin = document.getElementById("demo-contact-min").value;
  var hourVal = document.getElementById("demo-hour").value;

  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  var now = new Date();
  var txnHour = hourVal !== "" ? parseInt(hourVal, 10) : now.getHours();
  var timeOfFailure =
    now.getFullYear() +
    "-" +
    pad(now.getMonth() + 1) +
    "-" +
    pad(now.getDate()) +
    "T" +
    pad(txnHour) +
    ":00:00Z";

  var lastContact = null;
  if (lastContactMin !== "") {
    var min = parseInt(lastContactMin, 10);
    var contactDate = new Date(Date.now() - min * 60 * 1000);
    lastContact =
      contactDate.getFullYear() +
      "-" +
      pad(contactDate.getMonth() + 1) +
      "-" +
      pad(contactDate.getDate()) +
      "T" +
      pad(contactDate.getHours()) +
      ":" +
      pad(contactDate.getMinutes()) +
      ":00Z";
  }

  var txnId = "TXN-DEMO-" + Math.floor(Math.random() * 90000 + 10000);
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

  // Pipeline step visualizer animation
  var updateStep = function (activeIdx) {
    for (var i = 1; i <= 6; i++) {
      var stepEl = document.getElementById("pipe-step-" + i);
      if (stepEl) {
        if (i < activeIdx) {
          stepEl.className = "progress-step progress-step--done";
        } else if (i === activeIdx) {
          stepEl.className = "progress-step progress-step--active";
        } else {
          stepEl.className = "progress-step";
        }
      }
    }
  };

  updateStep(2);
  setTimeout(function () { updateStep(3); }, 300);
  setTimeout(function () { updateStep(4); }, 650);

  try {
    var resp = await fetch("/api/simulate/single", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(await resp.text());
    var rec = await resp.json();

    setTimeout(function () { updateStep(5); }, 900);
    setTimeout(function () {
      updateStep(6);
      for (var i = 1; i <= 6; i++) {
        var s = document.getElementById("pipe-step-" + i);
        if (s) s.className = "progress-step progress-step--done";
      }
      renderResult(result, rec);
      btn.disabled = false;
      btn.innerHTML = '<span class="btn-symbol">▶</span> Execute Ingestion Case';
    }, 1200);
  } catch (err) {
    result.className = "case-file";
    result.innerHTML =
      '<div class="case-body" style="border:1px solid var(--color-overridden);color:var(--color-paper);">' +
      '<strong class="mono" style="color:var(--color-overridden)">EXECUTION ERROR:</strong> ' +
      escapeHtml(err.message) +
      "</div>";
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-symbol">▶</span> Execute Ingestion Case';
  }
}

/**
 * Render the decision strictly into one of four states:
 * 1. PROPOSED: model candidate (grey dashed border)
 * 2. OVERRIDDEN: model != final (struck out proposal + solid red final + rule ID)
 * 3. MANDATED: model == final and rule fired (amber/gold border + rule pill)
 * 4. CONFIRMED: no rule fired (solid slate-green border)
 */
function renderResult(container, rec) {
  var modelAction = (rec.model_action || "UNKNOWN").toUpperCase();
  var finalAction = (rec.final_action || "UNKNOWN").toUpperCase();
  var ruleId = rec.guardrail_rule_id || "";
  var reason = rec.override_reason || "";
  var wasOverridden = !!rec.was_overridden && (modelAction !== finalAction);
  var isMandated = !!ruleId && (modelAction === finalAction);
  var recovered = rec.simulated_outcome && rec.simulated_outcome.recovered;
  var recoveredAmt = parseFloat(
    (rec.simulated_outcome && rec.simulated_outcome.amount_recovered_inr) || 0
  );

  var decisionMarkup = "";
  var alertBanner = "";

  if (wasOverridden) {
    decisionMarkup =
      '<div class="decision-block is-overridden" ' +
      'data-action="' + escapeHtml(finalAction) + '" ' +
      'data-rule-id="' + escapeHtml(ruleId) + '" ' +
      'data-source="policy">' +
      '<span class="decision-state-tag">Guardrail Override:</span> ' +
      '<del class="action-struck" data-action="' + escapeHtml(modelAction) + '" data-source="model">' +
      escapeHtml(modelAction) +
      '</del> ' +
      '<span class="action-enforced" data-action="' + escapeHtml(finalAction) + '" data-source="policy">' +
      escapeHtml(finalAction) +
      '</span> ' +
      '<span class="rule-pill mono" data-rule-id="' + escapeHtml(ruleId) + '">' +
      escapeHtml(ruleId) +
      '</span>' +
      '</div>';

    alertBanner =
      '<div class="override-alert-banner" role="alert" aria-live="polite">' +
      '<div class="override-alert-header">' +
      '<span class="rule-pill mono" data-rule-id="' + escapeHtml(ruleId) + '">' + escapeHtml(ruleId) + '</span>' +
      '<span class="override-alert-title">Statutory Policy Engine Veto Applied (Action Changed)</span>' +
      '</div>' +
      '<div class="override-alert-reason">' + escapeHtml(reason) + '</div>' +
      '</div>';
  } else if (isMandated) {
    decisionMarkup =
      '<div class="decision-block is-mandated" ' +
      'data-action="' + escapeHtml(finalAction) + '" ' +
      'data-rule-id="' + escapeHtml(ruleId) + '" ' +
      'data-source="policy">' +
      '<span class="mandated-tag">Mandated (Concurs with Model):</span> ' +
      '<span class="action-mandated-name" data-action="' + escapeHtml(finalAction) + '" data-source="policy">' +
      escapeHtml(finalAction) +
      '</span> ' +
      '<span class="rule-pill-mandated mono" data-rule-id="' + escapeHtml(ruleId) + '">' +
      escapeHtml(ruleId) +
      '</span>' +
      '</div>';

    alertBanner =
      '<div class="mandated-alert-banner" role="alert" aria-live="polite">' +
      '<div class="mandated-alert-header">' +
      '<span class="rule-pill-mandated mono" data-rule-id="' + escapeHtml(ruleId) + '">' + escapeHtml(ruleId) + '</span>' +
      '<span class="mandated-alert-title">Statutory Rule Mandated (Model Independently Agreed)</span>' +
      '</div>' +
      '<div class="override-alert-reason">' + escapeHtml(reason) + '</div>' +
      '</div>';
  } else {
    decisionMarkup =
      '<div class="decision-block is-confirmed" ' +
      'data-action="' + escapeHtml(finalAction) + '" ' +
      'data-source="policy">' +
      '<span class="confirmed-tag">Confirmed:</span> ' +
      '<span class="action-confirmed-name" data-action="' + escapeHtml(finalAction) + '" data-source="policy">' +
      escapeHtml(finalAction) +
      '</span>' +
      '</div>';
  }

  var outcomeMarkup = recovered
    ? '<span class="data-value" style="color:var(--color-confirmed);font-weight:600;">✓ RECOVERED ₹' +
      recoveredAmt.toLocaleString("en-IN", { maximumFractionDigits: 0 }) +
      '</span>'
    : '<span class="data-value" style="color:var(--color-paper-muted);">✗ NOT RECOVERED THIS ATTEMPT</span>';

  var rationale = rec.explanation && rec.explanation.rationale ? rec.explanation.rationale : "Deterministic policy rationale recorded.";
  var caveat = rec.explanation && rec.explanation.confidence_caveat ? rec.explanation.confidence_caveat : "";
  var fallbackIfWrong = rec.explanation && rec.explanation.fallback_if_wrong ? rec.explanation.fallback_if_wrong : "";

  container.className = "case-file";
  container.innerHTML =
    '<header class="case-header">' +
    '<h3 class="case-title"><span class="case-seq mono">DOSSIER #' + escapeHtml(rec.txn_id) + '</span> Execution Result</h3>' +
    '<span class="data-value text-muted timestamp">' + new Date().toISOString() + '</span>' +
    '</header>' +
    '<div class="case-body">' +
    alertBanner +
    '<div class="pipeline-sequence">' +

    // Stage 1
    '<section class="pipeline-stage">' +
    '<div class="stage-meta"><span class="stage-number">1</span><h4 class="stage-title">Ingestion Event</h4></div>' +
    '<dl class="dossier-grid">' +
    '<div class="dossier-item"><dt>Transaction ID</dt><dd class="mono txn-id">' + escapeHtml(rec.txn_id) + '</dd></div>' +
    '<div class="dossier-item"><dt>Amount at Risk</dt><dd class="mono">₹' + parseFloat(rec.amount_inr).toLocaleString("en-IN") + '</dd></div>' +
    '<div class="dossier-item"><dt>Failure Code</dt><dd class="mono">' + escapeHtml(rec.failure_code) + '</dd></div>' +
    '<div class="dossier-item"><dt>Payment Rail</dt><dd class="mono">' + escapeHtml(rec.payment_method) + '</dd></div>' +
    '</dl>' +
    '</section>' +

    // Stage 2
    '<section class="pipeline-stage">' +
    '<div class="stage-meta"><span class="stage-number">2</span><h4 class="stage-title">Risk Model Inference</h4><span class="stage-role-tag role--ml">XGBoost Uplift</span></div>' +
    '<div class="decision-block decision-state--proposed" data-action="' + escapeHtml(modelAction) + '" data-source="model">' +
    '<span class="decision-state-tag">Model Proposed:</span> ' +
    '<strong class="mono">' + escapeHtml(modelAction) + '</strong> ' +
    '<span class="mono text-muted">(Confidence: ' + Math.round((rec.model_confidence || 0) * 100) + '%)</span>' +
    '</div>' +
    '</section>' +

    // Stage 3: Policy Engine Candidate Action
    '<section class="pipeline-stage">' +
    '<div class="stage-meta"><span class="stage-number">3</span><h4 class="stage-title">Policy Engine (Candidate Action)</h4><span class="stage-role-tag role--ml">Pre-Guardrail Check</span></div>' +
    '<div class="decision-block decision-state--proposed" data-action="' + escapeHtml(modelAction) + '" data-source="policy">' +
    '<span class="decision-state-tag">Candidate Submitted to Guardrail:</span> ' +
    '<strong class="mono">' + escapeHtml(modelAction) + '</strong>' +
    '</div>' +
    '</section>' +

    // Stage 4: Guardrail Veto (Final Decision)
    '<section class="pipeline-stage">' +
    '<div class="stage-meta"><span class="stage-number">4</span><h4 class="stage-title">Guardrail Veto (Final Decision)</h4><span class="stage-role-tag role--veto">Veto Authority</span></div>' +
    decisionMarkup +
    '</section>' +

    // Stage 5: Explanation Layer
    '<section class="pipeline-stage">' +
    '<div class="stage-meta"><span class="stage-number">5</span><h4 class="stage-title">Explanation Layer</h4><span class="stage-role-tag role--advisory">Advisory Only</span></div>' +
    '<article class="advisory-block" data-source="llm">' +
    '<div class="advisory-header">' +
    '<span class="advisory-title">Gemini 2.5 Flash Advisory Dossier</span>' +
    '<span class="advisory-disclaimer">Advisory only — not a decision</span>' +
    '</div>' +
    '<p class="advisory-prose">' + escapeHtml(rationale) + '</p>' +
    (caveat ? '<div class="advisory-caveat mono"><strong>Caveat:</strong> ' + escapeHtml(caveat) + '</div>' : '') +
    (fallbackIfWrong ? '<div class="advisory-fallback mono"><strong>Contingency:</strong> ' + escapeHtml(fallbackIfWrong) + '</div>' : '') +
    '</article>' +
    '</section>' +

    // Stage 6: Settlement & Audit Trail
    '<section class="pipeline-stage">' +
    '<div class="stage-meta"><span class="stage-number">6</span><h4 class="stage-title">Settlement &amp; Audit Trail</h4></div>' +
    '<dl class="dossier-grid">' +
    '<div class="dossier-item"><dt>Settlement Outcome</dt><dd>' + outcomeMarkup + '</dd></div>' +
    '<div class="dossier-item"><dt>Recovery Probability Used</dt><dd class="mono">' + Math.round(((rec.simulated_outcome && rec.simulated_outcome.recovery_probability_used) || 0) * 100) + '%</dd></div>' +
    '<div class="dossier-item"><dt>Audit Record</dt><dd><a href="/transaction/' + escapeHtml(rec.txn_id) + '" class="mono" style="color:var(--color-paper);text-decoration:underline;">Inspect Full Ledger Entry →</a></dd></div>' +
    '</dl>' +
    '</section>' +

    '</div>' +
    '</div>';
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

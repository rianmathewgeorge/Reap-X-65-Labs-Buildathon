const $ = (id) => document.getElementById(id);
const requestIdPattern = /^[a-f0-9-]{36}$/;
const queryRequestId = new URLSearchParams(location.search).get("request_id");
let requestId = requestIdPattern.test(queryRequestId || "") ? queryRequestId : null;
if (!requestId && requestIdPattern.test(sessionStorage.getItem("spendpilot_request_id") || "")) requestId = sessionStorage.getItem("spendpilot_request_id");
let scope = null;
let activeRecord = null;
let inFlight = false;
let restored = Boolean(requestId);
const ruleNames = {scope_confirmed: "Scope confirmed", active_trusted_enrollment: "Payment enrollment active", selected_product_binding: "Correct item and quantity", approved_merchant: "Approved merchant", availability: "Item available", market_and_currency: "Singapore · SGD", valid_quote: "Valid quote and expiry", quote_source: "Verified quote source", per_checkout_cap: "Within checkout limit", run_budget: "Within remaining budget", no_pending_attempt: "No duplicate purchase", verified_discovery: "Eligible quote required", quote_changed: "Quote changed", reap_readiness: "Quote required", reap_refresh: "Current payment details required", operator_confirmation: "Your confirmation required", atomic_reservation: "Budget reservation"};
const toolNames = {interpret_request: "Interpret request", search_products: "Search catalog", get_product: "Check item details", create_quote: "Get final quote", get_enrollment: "Check payment enrollment"};
const eventNames = {scope_confirmed: "Spending scope confirmed", discovery: "Catalog and quote checked", discovery_evaluated: "Policy decision recorded", blocked: "Purchase blocked", discovery_stopped: "Discovery stopped", quote_refreshed: "Quote checked again", checkout_evaluated: "Checkout policy checked", budget_reserved: "Budget reserved", checkout_response: "Checkout response received", checkout_status_refreshed: "Checkout status checked", checkout_blocked: "Checkout blocked", quote_changed: "Quote changed · review needed", checkout_outcome_unknown: "Checkout outcome uncertain", settlement_unverified: "Settlement unverified", settlement_discrepancy: "Settlement amount differs", hosted_approval_unverified: "Hosted approval needs clarification", status_lookup_unavailable: "Status lookup unavailable"};

const money = (minor) => typeof minor === "number" ? `S$${(minor / 100).toFixed(2)}` : "Unknown";
const time = (value) => value && !Number.isNaN(Date.parse(value)) ? `${new Intl.DateTimeFormat("en-SG", {dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Singapore"}).format(new Date(value))} SGT` : "Unknown";
const text = (tag, value, className = "") => { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; return node; };
const clear = (node) => node.replaceChildren();
const csrf = () => document.cookie.split("; ").find((part) => part.startsWith("spendpilot_csrf="))?.split("=")[1];
const validRules = (rules) => Array.isArray(rules) && rules.length > 0 && rules.every((rule) => rule.outcome === "PASS");
const eligible = (record = activeRecord) => record?.state === "QUOTED" && validRules(record.rule_results);
const isFixture = (record = activeRecord) => record?.source_mode === "policy_test" || scope?.mode === "policy_test";

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.method === "POST") Object.assign(headers, {"Content-Type": "application/json", "X-CSRF-Token": csrf(), Origin: window.location.origin});
  const response = await fetch(path, {...options, headers});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || body.detail || "Request failed");
  return body;
}

function updateJourney(step, title, detail) {
  $("current-step-number").textContent = String(step);
  $("current-step").textContent = title;
  $("current-step-detail").textContent = detail;
  document.querySelectorAll(".progress-rail div").forEach((item, index) => {
    const current = index + 1 === step;
    item.classList.toggle("is-current", current);
    item.classList.toggle("is-complete", !current && (index === 0 && Boolean(activeRecord) || index === 1 && Boolean(activeRecord?.attempt)));
    if (current) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
  });
}

function stateClass(state = "") {
  if (["COMPLETED", "QUOTED"].includes(state)) return "is-positive";
  if (["BLOCKED", "FAILED", "EXPIRED"].includes(state)) return "is-danger";
  if (["UNKNOWN", "PROCESSING", "REQUIRES_ACTION", "REVIEW_REQUIRED"].includes(state)) return "is-warning";
  return "";
}

function stateLabel(state = "") { return state.replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase()) || "Awaiting review"; }

function syncControls() {
  const ready = Boolean(scope?.enrollment_ready) && !scope.suspended;
  $("start").disabled = inFlight || Boolean(requestId) || !ready || !$("scope-confirm").checked;
  $("new-request").disabled = inFlight || ["UNKNOWN", "RESERVED"].includes(activeRecord?.attempt?.checkout_status);
  $("checkout").disabled = inFlight || !ready || !eligible();
  $("refresh").disabled = inFlight || !activeRecord?.attempt;
  $("resume-approval").disabled = inFlight || !scope?.enrollment_ready;
  document.body.classList.toggle("is-busy", inFlight);
}

async function busy(task) {
  if (inFlight) return;
  inFlight = true;
  syncControls();
  try { return await task(); } finally { inFlight = false; syncControls(); }
}

function setSelection(id, {writeUrl = false} = {}) {
  requestId = id;
  if (id) {
    sessionStorage.setItem("spendpilot_request_id", id);
    if (writeUrl) {
      const url = new URL(location.href); url.searchParams.set("request_id", id);
      history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }
  } else sessionStorage.removeItem("spendpilot_request_id");
  syncControls();
}

function clearSelection() {
  requestId = null; activeRecord = null; restored = false; sessionStorage.removeItem("spendpilot_request_id");
  const url = new URL(location.href); url.searchParams.delete("request_id");
  history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

function showComposer(show) {
  $("composer").classList.toggle("hidden", !show);
  $("restored-run").classList.toggle("hidden", !requestId);
  $("run-title").textContent = restored ? "Saved purchase restored" : "Current purchase request";
  $("run-detail").textContent = restored ? "The result below belongs to your saved request." : "Follow the decision and available actions below.";
  document.body.classList.toggle("has-record", Boolean(requestId));
}

function setOutcome(title, detail, kind = "") {
  const outcome = $("checkout-status"); clear(outcome); outcome.className = `outcome ${kind}`.trim();
  if (!title) return;
  outcome.append(text("strong", title), text("span", detail));
}

function showRules(rules = []) {
  const container = $("rules"); clear(container);
  const passed = rules.filter(rule => rule.outcome === "PASS").length;
  container.append(text("p", rules.length ? `${passed} of ${rules.length} required checks passed` : "Policy checks have not been verified for this state.", `policy-summary ${validRules(rules) ? "pass" : "attention"}`));
  if (!rules.length) return;
  const details = document.createElement("details"); details.open = !validRules(rules);
  details.append(text("summary", validRules(rules) ? "View all policy checks" : "Review the checks that need attention"));
  const list = document.createElement("div"); list.className = "rule-list";
  rules.forEach((rule) => {
    const row = document.createElement("div"); const outcome = String(rule.outcome || "UNKNOWN").toLowerCase();
    row.className = `rule ${outcome}`;
    row.append(text("b", rule.outcome || "UNKNOWN"), text("strong", ruleNames[rule.rule] || String(rule.rule || "Policy check").replaceAll("_", " ")), text("small", rule.detail || "No further detail."));
    list.append(row);
  });
  details.append(list); container.append(details);
}

function showTools(events = []) {
  const tools = $("tools"); clear(tools);
  events.forEach((event) => tools.append(text("span", `${toolNames[event.tool] || String(event.tool || "Tool").replaceAll("_", " ")} · ${event.status || "completed"}`)));
  if (!events.length) tools.append(text("span", "No new external tool activity recorded."));
}

function showQuote(record) {
  const quote = $("quote"); clear(quote);
  if (!record.candidate || !record.quote) return;
  const card = document.createElement("div"); card.className = "quote-card";
  const meta = document.createElement("div"); meta.className = "quote-meta";
  meta.append(text("h3", record.candidate.name || "Product name unknown"), text("p", `${record.candidate.merchant_name || "Merchant name unknown"} · Quantity 1`));
  const price = document.createElement("div"); price.className = "quote-price"; price.append(text("strong", money(record.quote.total_minor)), text("span", `${record.quote.currency || "Currency unknown"} total`));
  const breakdown = document.createElement("div"); breakdown.className = "quote-breakdown";
  [["Subtotal", record.quote.subtotal_minor], ["Delivery", record.quote.shipping_minor], ["Tax", record.quote.tax_minor], ["Expires", time(record.quote.expires_at)]].forEach(([label, value]) => { const part = text("span", ""); part.append(text("b", `${label}: `), document.createTextNode(label === "Expires" ? value : money(value))); breakdown.append(part); });
  card.append(meta, price, breakdown);
  if (isFixture(record)) card.append(text("p", "POLICY TEST · NOT LIVE REAP", "fixture-label"));
  card.append(text("p", "Hardware specifications and missing shipping or tax details remain unknown. The final quote total controls the budget.", "unknown-note"));
  quote.append(card);
}

function showAttempt(record, {nextActionUrl = null, restoredRun = false} = {}) {
  const attempt = record.attempt;
  if (!attempt) {
    if (record.state === "BLOCKED") setOutcome("Purchase blocked before checkout", "The server confirmed no checkout was created.", "is-danger");
    else if (record.state === "REVIEW_REQUIRED") setOutcome("Quote needs a new review", "The stored quote changed before checkout could be prepared.", "is-warning");
    else if (record.state === "UNKNOWN") setOutcome("Checkout status is unknown", "Keep the existing reservation in place and inspect the recorded evidence before taking any further action.", "is-warning");
    return;
  }
  $("refresh").classList.remove("hidden");
  const status = attempt.checkout_status || "UNKNOWN";
  if (status === "COMPLETED" && record.source_mode === "live") setOutcome("Sandbox checkout completed", `Order ${attempt.order_id || "unknown"}; actual amount ${money(attempt.final_amount_minor)}.`, "is-positive");
  else if (status === "UNKNOWN") setOutcome("Checkout status is unknown", "The reservation remains held. Do not start another checkout for this request.", "is-warning");
  else if (status === "REQUIRES_ACTION" && isFixture(record)) setOutcome("Fixture record requires action", "POLICY TEST only. No hosted approval link or payment was created.", "is-warning");
  else if (status === "REQUIRES_ACTION") setOutcome("Approval is required in Reap", nextActionUrl ? "Open the hosted Reap approval when you are ready, then refresh this status." : "No hosted approval link is currently available. Use Resume Reap approval only to retrieve the existing attempt.", "is-warning");
  else setOutcome(`Checkout ${stateLabel(status)}`, "Refresh status after the relevant Reap action. A redirect alone does not confirm payment.", stateClass(status));
  if (attempt.settlement_state === "HELD" && status === "COMPLETED") setOutcome("Settlement needs verification", "The reservation remains held and new purchases are suspended until the actual amount is verified.", "is-warning");
  if (nextActionUrl && record.source_mode === "live") {
    try { const url = new URL(nextActionUrl); if (url.protocol === "https:" && url.hostname) { const link = text("a", "Open Reap approval ↗"); link.href = url.href; link.rel = "noreferrer"; $("checkout-status").append(link); } } catch { /* Server response did not provide a usable hosted URL. */ }
  }
  const resume = restoredRun && record.source_mode === "live" && status === "REQUIRES_ACTION" && !nextActionUrl;
  $("resume-approval").classList.toggle("hidden", !resume);
}

function renderRecord(record, options = {}) {
  activeRecord = record;
  $("discovery").classList.remove("hidden");
  $("request-state").textContent = stateLabel(record.state);
  $("request-state").className = `state-pill ${stateClass(record.state)}`.trim();
  $("review-summary").textContent = eligible(record) ? "The quote meets every current policy check. You still choose whether to prepare a Reap approval." : "The latest persisted evidence for this request.";
  showTools(options.toolEvents || []); showQuote(record); showRules(record.rule_results || []); setOutcome("", "");
  $("checkout").classList.toggle("hidden", !eligible(record));
  $("refresh").classList.toggle("hidden", !record.attempt);
  $("resume-approval").classList.add("hidden");
  showAttempt(record, options);
  const status = record.attempt?.checkout_status || record.state;
  const step = ["REQUIRES_ACTION", "PROCESSING", "UNKNOWN"].includes(status) ? 3 : ["COMPLETED", "FAILED", "EXPIRED"].includes(status) ? 4 : record.state === "QUOTED" || record.state === "BLOCKED" || record.state === "REVIEW_REQUIRED" ? 2 : 1;
  const stepTitle = status === "UNKNOWN" ? "Resolve checkout status" : status === "PROCESSING" ? "Checkout is processing" : step === 1 ? "Confirm request" : step === 2 ? "Review quote" : step === 3 ? (isFixture(record) ? "Policy-test result" : "Approve in Reap") : "Review outcome";
  updateJourney(step, stepTitle, status === "UNKNOWN" ? "Reservation held · do not retry" : isFixture(record) && record.attempt ? "Synthetic record · no real payment" : step === 3 ? "Completion is not yet confirmed" : "Choose your next explicit step");
  syncControls();
}

async function showPassport(id = requestId) {
  if (!id) return;
  const passport = await api(`/api/requests/${id}/passport`);
  if (id !== requestId) return;
  const timeline = $("timeline"); clear(timeline); $("passport").classList.remove("hidden");
  if (passport.fixture_notice) timeline.append(text("li", passport.fixture_notice, "fixture-label"));
  passport.events.forEach((event) => {
    const item = document.createElement("li"); item.append(text("span", event.source || "APP_POLICY", "evidence-source"), text("strong", eventNames[event.event_type] || String(event.event_type || "Recorded event").replaceAll("_", " ")), text("small", time(event.created_at)));
    if (event.payload && typeof event.payload === "object") { const details = document.createElement("details"); details.append(text("summary", "View recorded evidence"), text("pre", JSON.stringify(event.payload, null, 2))); item.append(details); }
    timeline.append(item);
  });
}

function renderScope(value) {
  scope = value;
  $("per-checkout").textContent = money(value.per_checkout_cap_minor);
  $("run-budget").textContent = money(value.budget?.available_minor);
  $("merchant").textContent = value.merchant_key || "Awaiting confirmation";
  $("market").textContent = `${value.market || "Unknown"} · ${value.currency || "Unknown"}`;
  $("sidebar-budget").textContent = money(value.budget?.available_minor);
  $("reserved-budget").textContent = money(value.budget?.reserved_minor);
  $("completed-budget").textContent = money(value.budget?.completed_minor);
  const total = value.budget?.total_budget_minor || 0; const available = value.budget?.available_minor || 0;
  $("budget-meter").style.width = `${total > 0 ? Math.max(0, Math.min(100, available / total * 100)) : 0}%`;
  $("budget-note").textContent = value.suspended ? "Purchases are suspended while the current record is reviewed." : `S$${((total || 0) / 100).toFixed(2)} total run limit`;
  $("mode").textContent = value.mode === "policy_test" ? "Policy test · not live Reap" : "Live Reap sandbox";
  $("demo-hint").classList.toggle("hidden", value.mode !== "policy_test");
  if (!value.enrollment_ready) $("request-status").textContent = "Reap setup is not ready yet. Quote search will be available after integration.";
  syncControls();
}

async function loadScope() { renderScope(await api("/api/scope")); }

async function restoreRun() {
  if (!requestId) { showComposer(true); updateJourney(1, "Confirm request", "Choose an equipment request to begin"); return; }
  showComposer(false);
  $("discovery").classList.remove("hidden"); setOutcome("Loading saved request", "Retrieving its recorded state…");
  const id = requestId;
  try { const record = await api(`/api/requests/${id}`); if (id !== requestId) return; renderRecord(record, {restoredRun: true}); await showPassport(id); }
  catch { if (id === requestId) { clearSelection(); showComposer(true); $("request-status").textContent = "The saved request was unavailable. Start a new request to continue."; } }
}

async function requestCheckout() {
  if (!requestId || inFlight || !scope?.enrollment_ready || (eligible() && scope.suspended) || (!eligible() && !(activeRecord?.source_mode === "live" && activeRecord?.attempt?.checkout_status === "REQUIRES_ACTION"))) return;
  const id = requestId;
  await busy(async () => {
    const result = await api(`/api/requests/${id}/checkout`, {method: "POST", body: JSON.stringify({confirm: true})});
    if (id !== requestId) return;
    const record = {...activeRecord, state: result.state, attempt: result.attempt || activeRecord?.attempt, rule_results: result.rule_results || activeRecord?.rule_results};
    renderRecord(record, {nextActionUrl: result.next_action_url || null});
    await showPassport(id); await loadScope();
  });
}

$("scope-confirm").addEventListener("change", syncControls);
$("start").addEventListener("click", () => busy(async () => {
  if (!scope?.enrollment_ready || scope.suspended || requestId || !$("scope-confirm").checked) return;
  $("request-status").textContent = "Saving your confirmed request…";
  const created = await api("/api/requests", {method: "POST", body: JSON.stringify({text: $("request").value})});
  restored = false; setSelection(created.request_id, {writeUrl: true}); showComposer(false);
  $("discovery").classList.remove("hidden"); $("passport").classList.add("hidden");
  $("request-state").textContent = "Working"; $("request-state").className = "state-pill";
  setOutcome("Preparing your quote", "Checking your request against the approved scope. Tool activity appears after it is recorded.");
  updateJourney(2, "Preparing quote", "You will review the result before checkout");
  const result = await api(`/api/requests/${requestId}/discover`, {method: "POST"});
  renderRecord({...result, source_mode: scope.mode}, {toolEvents: result.tool_events || []}); await showPassport();
  $("request-status").textContent = result.state === "BLOCKED" ? "The request was blocked before checkout." : "Quote ready for your review.";
}).catch((error) => {
  if (requestId) { $("discovery").classList.remove("hidden"); setOutcome("Quote preparation stopped", `${error.message} Start a new request when ready.`, "is-warning"); }
  else $("request-status").textContent = error.message;
}));
$("checkout").addEventListener("click", () => requestCheckout().catch((error) => setOutcome("Checkout could not be prepared", error.message, "is-danger")));
$("resume-approval").addEventListener("click", () => requestCheckout().catch((error) => setOutcome("Approval could not be resumed", error.message, "is-danger")));
$("refresh").addEventListener("click", () => busy(async () => { if (!requestId) return; const record = await api(`/api/requests/${requestId}/refresh`, {method: "POST", body: "{}"}); renderRecord(record); await showPassport(); await loadScope(); }).catch((error) => setOutcome("Status could not be refreshed", error.message, "is-danger")));
$("new-request").addEventListener("click", () => { if (inFlight || ["UNKNOWN", "RESERVED"].includes(activeRecord?.attempt?.checkout_status)) return; const uncertain = activeRecord?.attempt?.checkout_status === "UNKNOWN"; clearSelection(); clear($("quote")); clear($("rules")); clear($("tools")); clear($("timeline")); $("discovery").classList.add("hidden"); $("passport").classList.add("hidden"); $("scope-confirm").checked = false; $("request-status").textContent = uncertain ? "The previous reservation remains held. This starts a separate request; it does not retry the uncertain checkout." : ""; showComposer(true); updateJourney(1, "Confirm request", "Review the scope and begin a new request"); $("request").focus(); syncControls(); });

busy(async () => { try { await loadScope(); } catch { $("mode").textContent = "Readiness unavailable"; $("budget-note").textContent = "Trusted scope could not be loaded. Actions remain disabled."; } await restoreRun(); });

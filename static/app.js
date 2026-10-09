let requestId = new URLSearchParams(location.search).get("request_id") || sessionStorage.getItem("spendpilot_request_id");
if (requestId && !/^[a-f0-9-]{36}$/.test(requestId)) requestId = null;
const $ = (id) => document.getElementById(id);
const money = (minor) => typeof minor === "number" ? `S$${(minor / 100).toFixed(2)}` : "Unknown";
const time = (value) => value && !Number.isNaN(Date.parse(value)) ? new Intl.DateTimeFormat("en-SG", {dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Singapore"}).format(new Date(value)) + " SGT" : "Unknown";
const csrf = () => document.cookie.split("; ").find((part) => part.startsWith("spendpilot_csrf="))?.split("=")[1];
const clear = (node) => node.replaceChildren();
const text = (tag, value, className = "") => { const node = document.createElement(tag); node.textContent = value; node.className = className; return node; };
const setBusy = (button, busy) => { button.disabled = busy; button.dataset.original ??= button.textContent; button.textContent = busy ? "Working…" : button.dataset.original; };

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.method === "POST") Object.assign(headers, {"Content-Type": "application/json", "X-CSRF-Token": csrf(), "Origin": window.location.origin});
  const response = await fetch(path, {...options, headers});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || body.detail || "Request failed");
  return body;
}

function showRules(rules = []) {
  const container = $("rules"); clear(container);
  rules.forEach((rule) => { const row = text("p", "", `rule ${String(rule.outcome || "UNKNOWN").toLowerCase()}`); row.append(text("b", rule.outcome || "UNKNOWN"), document.createTextNode(` ${String(rule.rule || "policy").replaceAll("_", " ")}`), text("small", rule.detail || "No further detail.")); container.append(row); });
}

function showAttempt(record) {
  const attempt = record.attempt;
  const quote = $("quote"); clear(quote);
  $("discovery").classList.remove("hidden");
  if (!attempt && record.candidate && record.quote) { showDiscovery({...record, tool_events: [], fixture_notice: record.source_mode === "policy_test" ? "POLICY TEST - NOT LIVE REAP" : null}); }
  else if (!attempt) quote.append(text("p", record.state === "BLOCKED" ? "BLOCKED · No checkout created." : "No checkout attempt exists. A page load never creates one."));
  else {
    quote.append(text("h3", `Checkout status: ${attempt.checkout_status}`));
    if (attempt.checkout_status === "COMPLETED" && record.source_mode === "live") quote.append(text("p", `Sandbox checkout completed. Order ${attempt.order_id || "unknown"}; actual amount ${money(attempt.final_amount_minor)}.`));
    else quote.append(text("p", record.source_mode === "policy_test" ? "POLICY TEST - NOT LIVE REAP. Fixture record only; no hosted approval or payment occurred." : "A return redirect alone is not payment completion. Refresh status after a hosted Reap action."));
    if (attempt.settlement_state === "HELD" && attempt.checkout_status === "COMPLETED") quote.append(text("p", "Actual settlement is unverified; reservation held and new purchases suspended.", "notice"));
    $("refresh").classList.remove("hidden");
  }
}

async function showPassport() {
  const passport = await api(`/api/requests/${requestId}/passport`), timeline = $("timeline");
  $("passport").classList.remove("hidden"); clear(timeline);
  if (passport.fixture_notice) timeline.append(text("li", passport.fixture_notice, "notice"));
  passport.events.forEach((event) => {
    const item = text("li", ""); item.append(text("b", event.source || "APP_POLICY"), document.createTextNode(` · ${event.event_type || "event"}`), text("small", time(event.created_at)));
    if (event.payload && typeof event.payload === "object") { const details = document.createElement("details"); details.append(text("summary", "View recorded evidence"), text("pre", JSON.stringify(event.payload, null, 2))); item.append(details); }
    timeline.append(item);
  });
}

function showDiscovery(result) {
  $("discovery").classList.remove("hidden");
  const tools = $("tools"); clear(tools); result.tool_events.forEach((event) => tools.append(text("span", `${event.tool.replaceAll("_", " ")}: ${event.status}`)));
  if (!result.tool_events.length) tools.append(text("span", "No external tool ran."));
  const quote = $("quote"); clear(quote);
  if (result.candidate && result.quote) {
    quote.append(text("h3", result.candidate.name || "Unknown product"), text("p", `${result.candidate.merchant_name || "Merchant unknown"} · Quantity 1`), text("p", `${money(result.quote.total_minor)} ${result.quote.currency} · expires ${time(result.quote.expires_at)}`, "amount"));
    quote.append(text("p", `Subtotal ${money(result.quote.subtotal_minor)} · Shipping ${money(result.quote.shipping_minor)} · Tax ${money(result.quote.tax_minor)}`), text("small", "Hardware specifications and missing shipping/tax details remain unknown. The final quote total controls the budget."));
    if (result.fixture_notice) quote.append(text("p", result.fixture_notice, "notice"));
  } else quote.append(text("p", "No policy-compliant candidate was prepared."));
  showRules(result.rule_results); $("checkout").classList.toggle("hidden", !result.rule_results.length || !result.rule_results.every((rule) => rule.outcome === "PASS"));
}

async function loadScope() {
  const scope = await api("/api/scope");
  $("per-checkout").textContent = money(scope.per_checkout_cap_minor);
  $("run-budget").textContent = money(scope.budget?.available_minor);
  $("merchant").textContent = scope.merchant_key || "Awaiting team-key confirmation";
  $("market").textContent = `${scope.market || "Unknown"} · ${scope.currency || "Unknown"}`;
  $("mode").textContent = `${scope.mode === "policy_test" ? "POLICY TEST · NOT LIVE REAP" : "LIVE REAP SANDBOX"} · ${scope.agent_mode}`;
  if (scope.suspended) $("mode").textContent += " · PURCHASES SUSPENDED";
}

$("scope-confirm").addEventListener("change", () => { $("start").disabled = !$("scope-confirm").checked; });
$("start").addEventListener("click", async () => {
  const start = $("start"); setBusy(start, true); $("checkout").classList.add("hidden"); $("refresh").classList.add("hidden"); clear($("quote")); clear($("rules")); clear($("checkout-status"));
  try {
    $("request-status").textContent = "Creating a server-held request…";
    const created = await api("/api/requests", {method: "POST", body: JSON.stringify({text: $("request").value})});
    requestId = created.request_id; sessionStorage.setItem("spendpilot_request_id", requestId);
    $("request-status").textContent = "Searching Reap and preparing a quote…";
    const result = await api(`/api/requests/${requestId}/discover`, {method: "POST"}); showDiscovery(result); await showPassport();
    $("request-status").textContent = result.state === "BLOCKED" ? "BLOCKED · No checkout created." : "Quote ready for human review.";
  } catch (error) { $("request-status").textContent = error.message; } finally { setBusy(start, false); }
});

$("checkout").addEventListener("click", async () => {
  const checkout = $("checkout"); setBusy(checkout, true);
  try {
    const result = await api(`/api/requests/${requestId}/checkout`, {method: "POST", body: JSON.stringify({confirm: true})});
    const status = $("checkout-status"); clear(status); status.append(document.createTextNode(`${result.state}: ${result.reasons?.join(", ") || "server-side checkout record created"}`));
    if (result.next_action_url) { const link = text("a", "Open Reap approval"); link.href = result.next_action_url; link.rel = "noreferrer"; status.append(document.createTextNode(" "), link); }
    if (result.attempt) { $("refresh").classList.remove("hidden"); if (result.state !== "REQUIRES_ACTION") showAttempt(result); }
    $("checkout").classList.add("hidden"); await showPassport(); await loadScope();
  } catch (error) { $("checkout-status").textContent = error.message; } finally { setBusy(checkout, false); }
});

$("refresh").addEventListener("click", async () => {
  const refresh = $("refresh"); setBusy(refresh, true);
  try { const record = await api(`/api/requests/${requestId}/refresh`, {method: "POST", body: "{}"}); showAttempt(record); await showPassport(); await loadScope(); }
  catch (error) { $("checkout-status").textContent = error.message; } finally { setBusy(refresh, false); }
});

loadScope().catch(() => { $("mode").textContent = "Readiness unavailable"; });
if (requestId) { sessionStorage.setItem("spendpilot_request_id", requestId); api(`/api/requests/${requestId}`).then((record) => { showAttempt(record); return showPassport(); }).catch(() => { sessionStorage.removeItem("spendpilot_request_id"); requestId = null; }); }

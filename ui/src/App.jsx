import { useCallback, useEffect, useState } from "react";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const PAGE_SIZE = 50;
const STATUSES = ["queued", "running", "succeeded", "failed", "cancelled"];
const OPERATORS = ["required", "regex", "one_of", "min_length", "max_length", "min", "max", "equals_field"];
const blankWorkflow = () => ({
  slug: "",
  name: "",
  version: 1,
  agent_profile: "",
  required_fields: "",
  required_evidence_types: "",
  require_evidence: false,
});
const blankRule = () => ({
  key: "",
  field: "",
  operator: "required",
  value: "",
  severity: "error",
  message: "",
});
const blankSop = () => ({
  title: "",
  content: "",
  candidate_fields: "",
  confirm_no_phi_or_secrets: false,
  confirm_candidate_fields: false,
});
const newMapping = (target = "", locator = "") => ({
  id: `${Date.now()}-${Math.random()}`,
  target,
  locator,
});
const newIntakeSource = (index, existingIds = []) => {
  const baseId = index === 0 ? "client_platform" : index === 1 ? "payer_roster" : `source_${index + 1}`;
  let sourceId = baseId;
  let suffix = 2;
  while (existingIds.includes(sourceId)) sourceId = `${baseId}_${suffix++}`;
  return {
    id: `${Date.now()}-${Math.random()}`,
    source_id: sourceId,
    format: "json",
    evidence_type: "source-document",
    file: null,
    worksheet: "",
    row_number: 1,
    mappings: [newMapping()],
  };
};
const initialIntakeSources = [
  {
    ...newIntakeSource(0),
    evidence_type: "client-record",
    mappings: [
      newMapping("client.provider.npi", "/provider/npi"),
      newMapping("client.provider.name", "/provider/name"),
    ],
  },
  {
    ...newIntakeSource(1),
    format: "xlsx",
    evidence_type: "payer-roster",
    mappings: [
      newMapping("payer.provider.npi", "Provider NPI"),
      newMapping("payer.provider.name", "Provider Name"),
    ],
  },
];
const lines = (text) => text.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
const formatDate = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString();
};

function App() {
  const [tab, setTab] = useState(
    window.location.pathname === "/monitoring" ? "monitoring"
      : window.location.pathname === "/intake" ? "intake" : "workflows",
  );
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [connectionState, setConnectionState] = useState("Not connected");
  const [error, setError] = useState("");
  const [isTenantAdmin, setIsTenantAdmin] = useState(false);
  const [workflows, setWorkflows] = useState([]);
  const [cases, setCases] = useState([]);
  const [offset, setOffset] = useState(0);
  const [caseStatus, setCaseStatus] = useState("");
  const [workflow, setWorkflow] = useState(blankWorkflow);
  const [rules, setRules] = useState([]);
  const [profiles, setProfiles] = useState([]);
  const [workflowMessage, setWorkflowMessage] = useState("");
  const [sop, setSop] = useState(blankSop);
  const [sopWorkflowId, setSopWorkflowId] = useState("");
  const [sops, setSops] = useState([]);
  const [sopId, setSopId] = useState("");
  const [suggestionJobId, setSuggestionJobId] = useState("");
  const [suggestionResult, setSuggestionResult] = useState(null);
  const [selectedSuggestions, setSelectedSuggestions] = useState([]);
  const [sopMessage, setSopMessage] = useState("");
  const [currentCase, setCurrentCase] = useState(null);
  const [caseExtras, setCaseExtras] = useState({ agentRuns: [], audit: [] });
  const [review, setReview] = useState({
    decision: "approve",
    note: "",
    confirm_qc_reviewed: false,
    confirm_evidence_reviewed: false,
  });
  const [reviewMessage, setReviewMessage] = useState("");
  const [windowDays, setWindowDays] = useState("7");
  const [monitoring, setMonitoring] = useState(null);
  const [lastUpdated, setLastUpdated] = useState("");
  const [intakeWorkflowSlug, setIntakeWorkflowSlug] = useState("");
  const [intakeReference, setIntakeReference] = useState("");
  const [intakeSourceSystem, setIntakeSourceSystem] = useState("cross-platform-intake");
  const [intakeSources, setIntakeSources] = useState(initialIntakeSources);
  const [intakeConfirmed, setIntakeConfirmed] = useState(false);
  const [intakeMessage, setIntakeMessage] = useState("");
  const [intakeCase, setIntakeCase] = useState(null);

  const availableIntakeWorkflows = workflows.filter(
    (item, index, all) => all.findIndex((candidate) => candidate.slug === item.slug) === index,
  );

  const api = useCallback(async (path, options = {}) => {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
        ...options.headers,
      },
      cache: "no-store",
    });
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // Non-JSON errors retain their HTTP status.
      }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }, [apiKey]);

  const loadWorkflows = useCallback(async () => {
    const result = await api("/workflows");
    setWorkflows(result);
    return result;
  }, [api]);

  const loadCases = useCallback(async (nextOffset = offset, status = caseStatus) => {
    const statusQuery = status ? `&status=${encodeURIComponent(status)}` : "";
    const result = await api(`/cases?limit=${PAGE_SIZE}&offset=${nextOffset}${statusQuery}`);
    setCases(result);
    setOffset(nextOffset);
  }, [api, caseStatus, offset]);

  const loadSops = useCallback(async (workflowId = sopWorkflowId) => {
    if (!workflowId) {
      setSops([]);
      setSopId("");
      return [];
    }
    const result = await api(`/workflows/${encodeURIComponent(workflowId)}/sops`);
    setSops(result);
    setSopId((current) => result.some((item) => item.id === current) ? current : (result[0]?.id || ""));
    return result;
  }, [api, sopWorkflowId]);

  const submitIntake = async (event) => {
    event.preventDefault();
    setIntakeMessage("");
    setIntakeCase(null);
    if (!apiKey) {
      setIntakeMessage("Connect with a tenant administrator or operator API key first.");
      return;
    }
    if (!intakeConfirmed) {
      setIntakeMessage("Confirm that the uploaded files contain synthetic/test data only.");
      return;
    }
    if (!intakeWorkflowSlug) {
      setIntakeMessage("Choose a workflow before starting intake.");
      return;
    }
    if (intakeSources.length < 2) {
      setIntakeMessage("Add at least two sources to run a cross-platform comparison.");
      return;
    }
    if (intakeSources.some((source) => !source.file)) {
      setIntakeMessage("Choose one file for every source.");
      return;
    }
    if (intakeSources.some((source) => source.file.size > 786_432)) {
      setIntakeMessage("Each file must be no larger than 768 KB.");
      return;
    }
    const totalBytes = intakeSources.reduce((sum, source) => sum + source.file.size, 0);
    if (totalBytes > 850_000) {
      setIntakeMessage("The total upload limit is 850 KB. Remove files or select smaller exports.");
      return;
    }
    if (!intakeSourceSystem.trim()) {
      setIntakeMessage("Enter an intake source name.");
      return;
    }
    try {
      const manifest = {
        workflow_slug: intakeWorkflowSlug,
        source_system: intakeSourceSystem.trim(),
        external_reference: intakeReference.trim() || null,
        confirm_synthetic_data: true,
        sources: intakeSources.map((source) => {
          const sourceId = source.source_id.trim();
          if (!/^[A-Za-z][A-Za-z0-9_.-]{0,99}$/.test(sourceId)) {
            throw new Error(`Source ID "${source.source_id}" is invalid. Use letters, numbers, periods, underscores, or hyphens.`);
          }
          const fieldMap = Object.create(null);
          for (const mapping of source.mappings) {
            const target = mapping.target.trim();
            const locator = mapping.locator.trim();
            if (!target || !locator) throw new Error(`Complete every field mapping for ${source.source_id}.`);
            if (!/^[A-Za-z][A-Za-z0-9_.-]{0,99}$/.test(target) || locator.length > 512) {
              throw new Error(`A field mapping in ${source.source_id} has an invalid case field or is too long.`);
            }
            if (["json", "fhir_json"].includes(source.format) && !locator.startsWith("/")) {
              throw new Error(`${source.source_id} JSON mappings must start with '/'.`);
            }
            if (Object.hasOwn(fieldMap, target)) throw new Error(`The mapped field "${target}" is duplicated in ${source.source_id}.`);
            fieldMap[target] = locator;
          }
          if (!Object.keys(fieldMap).length) throw new Error(`Add at least one field mapping for ${source.source_id}.`);
          return {
            source_id: sourceId,
            filename: source.file.name,
            format: source.format,
            evidence_type: source.evidence_type.trim() || "source-document",
            field_map: fieldMap,
            ...(source.format === "xlsx" && source.worksheet.trim() ? { worksheet: source.worksheet.trim() } : {}),
            ...(source.format === "xlsx" ? { row_index: Number(source.row_number) - 1 } : {}),
          };
        }),
      };
      const sourceIds = manifest.sources.map((source) => source.source_id);
      if (new Set(sourceIds).size !== sourceIds.length) throw new Error("Each source must have a unique name.");
      const filenames = manifest.sources.map((source) => source.filename);
      if (new Set(filenames).size !== filenames.length) throw new Error("Choose files with unique filenames for each source.");
      const targetFields = manifest.sources.flatMap((source) => Object.keys(source.field_map));
      if (new Set(targetFields).size !== targetFields.length) throw new Error("Each source must map to different case fields.");
      const requestBytes = totalBytes
        + new Blob([JSON.stringify(manifest)]).size
        + intakeSources.length * 2048
        + 4096;
      if (requestBytes > 1_048_576) {
        setIntakeMessage("The complete upload and mapping exceed the API's 1 MiB request limit. Select smaller files or simplify the mappings.");
        return;
      }
      const body = new FormData();
      body.set("manifest", JSON.stringify(manifest));
      for (const source of intakeSources) body.append("files", source.file, source.file.name);
      const result = await api("/cases/intake", { method: "POST", body });
      setIntakeCase(result);
      setIntakeMessage(`Created case ${result.id}. Check the QC results below.`);
      await loadCases(0, "");
    } catch (submitError) {
      setIntakeMessage(submitError.message);
    }
  };

  const updateIntakeSource = (sourceId, updates) => {
    setIntakeSources((current) => current.map(
      (source) => source.id === sourceId ? { ...source, ...updates } : source,
    ));
    setIntakeCase(null);
    setIntakeMessage("");
  };
  const updateIntakeMapping = (sourceId, mappingId, key, value) => {
    setIntakeSources((current) => current.map((source) => source.id === sourceId ? {
      ...source,
      mappings: source.mappings.map((mapping) => mapping.id === mappingId ? { ...mapping, [key]: value } : mapping),
    } : source));
    setIntakeCase(null);
    setIntakeMessage("");
  };
  const addIntakeSource = () => {
    setIntakeSources((current) => [...current, newIntakeSource(current.length, current.map((source) => source.source_id))]);
    setIntakeMessage("");
  };
  const removeIntakeSource = (sourceId) => {
    setIntakeSources((current) => current.filter((source) => source.id !== sourceId));
    setIntakeCase(null);
    setIntakeMessage("");
  };
  const addIntakeMapping = (sourceId) => {
    setIntakeSources((current) => current.map((source) => source.id === sourceId
      ? { ...source, mappings: [...source.mappings, newMapping()] } : source));
    setIntakeCase(null);
    setIntakeMessage("");
  };
  const removeIntakeMapping = (sourceId, mappingId) => {
    setIntakeSources((current) => current.map((source) => source.id === sourceId
      ? { ...source, mappings: source.mappings.filter((mapping) => mapping.id !== mappingId) } : source));
    setIntakeCase(null);
    setIntakeMessage("");
  };

  const connect = async (event) => {
    event.preventDefault();
    const candidate = apiKeyInput.trim();
    setApiKeyInput("");
    setError("");
    if (!candidate) {
      setConnectionState("Enter an API key to connect.");
      return;
    }
    setApiKey(candidate);
    setConnectionState("Connecting…");
    try {
      const request = async (path) => {
        const response = await fetch(`${API_BASE_URL}${path}`, {
          headers: { Accept: "application/json", Authorization: `Bearer ${candidate}` },
          cache: "no-store",
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `Request failed (${response.status})`);
        }
        return response.json();
      };
      const [workflowResult, caseResult] = await Promise.all([
        request("/workflows"),
        request(`/cases?limit=${PAGE_SIZE}&offset=0`),
      ]);
      setWorkflows(workflowResult);
      setIntakeWorkflowSlug((current) => current || workflowResult[0]?.slug || "");
      setCases(caseResult);
      setOffset(0);
      try {
        const profileResult = await request("/model-profiles");
        setProfiles(profileResult);
        setIsTenantAdmin(true);
        setConnectionState("Connected · API key held in page memory only");
        setSopWorkflowId(workflowResult[0]?.id || "");
      } catch (profileError) {
        if (profileError.message !== "Insufficient role") throw profileError;
        setProfiles([]);
        setIsTenantAdmin(false);
        setConnectionState("Connected · reviewer access · API key held in page memory only");
      }
    } catch (connectError) {
      setApiKey("");
      setConnectionState("Connection failed");
      setError(connectError.message);
    }
  };

  const disconnect = () => {
    setApiKey("");
    setApiKeyInput("");
    setConnectionState("Not connected");
    setIsTenantAdmin(false);
    setWorkflows([]);
    setCases([]);
    setProfiles([]);
    setOffset(0);
    setCurrentCase(null);
    setMonitoring(null);
    setSops([]);
    setSuggestionResult(null);
    setSuggestionJobId("");
    setError("");
  };

  useEffect(() => {
    if (!apiKey) return;
    loadWorkflows().catch((loadError) => setError(loadError.message));
  }, [apiKey, loadWorkflows]);

  useEffect(() => {
    if (!apiKey || tab !== "workflows" || !isTenantAdmin || !sopWorkflowId) return;
    loadSops(sopWorkflowId).catch((loadError) => setError(loadError.message));
  }, [apiKey, isTenantAdmin, loadSops, sopWorkflowId, tab]);

  const refreshMonitoring = useCallback(async () => {
    if (!apiKey) return;
    try {
      const [summary, jobs, errors] = await Promise.all([
        api(`/monitoring/api/summary?window_days=${encodeURIComponent(windowDays)}`),
        api("/automation/jobs?limit=100"),
        api("/monitoring/api/errors?limit=100"),
      ]);
      setMonitoring({ summary, jobs, errors });
      setLastUpdated(new Date().toLocaleTimeString());
      setConnectionState("Connected · refreshing every 15 seconds");
    } catch (loadError) {
      setError(loadError.message);
      setConnectionState("Connection or authorization error");
    }
  }, [api, apiKey, windowDays]);

  useEffect(() => {
    if (!apiKey || tab !== "monitoring") return undefined;
    refreshMonitoring();
    const timer = window.setInterval(refreshMonitoring, 15000);
    return () => window.clearInterval(timer);
  }, [apiKey, refreshMonitoring, tab]);

  const navigate = (nextTab) => {
    setTab(nextTab);
    const path = nextTab === "monitoring" ? "/monitoring"
      : nextTab === "intake" ? "/intake" : "/client-portal";
    window.history.pushState({}, "", path);
  };

  useEffect(() => {
    const onPopState = () => setTab(
      window.location.pathname === "/monitoring" ? "monitoring"
        : window.location.pathname === "/intake" ? "intake" : "workflows",
    );
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const updateWorkflow = (event) => {
    const { name, value, type, checked } = event.target;
    setWorkflow((current) => ({ ...current, [name]: type === "checkbox" ? checked : value }));
  };

  const submitWorkflow = async (event) => {
    event.preventDefault();
    setWorkflowMessage("");
    try {
      const fieldRules = rules.map((rule) => {
        let value = rule.operator === "required" ? null : rule.value;
        if (rule.operator === "one_of") {
          value = JSON.parse(rule.value);
          if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) {
            throw new Error("For a “one of” rule, enter a JSON array of strings.");
          }
        } else if (["min", "max", "min_length", "max_length"].includes(rule.operator)) {
          value = Number(rule.value);
          if (!Number.isFinite(value)) throw new Error(`Enter a number for the ${rule.operator} rule.`);
        }
        return { ...rule, value, key: rule.key.trim(), field: rule.field.trim(), message: rule.message.trim() };
      });
      await api("/workflows", {
        method: "POST",
        body: JSON.stringify({
          slug: workflow.slug.trim(),
          name: workflow.name.trim(),
          version: Number(workflow.version),
          agent_profile: workflow.agent_profile,
          required_fields: lines(workflow.required_fields),
          required_evidence_types: lines(workflow.required_evidence_types),
          field_rules: fieldRules,
          require_evidence: workflow.require_evidence,
        }),
      });
      setWorkflow(blankWorkflow());
      setRules([]);
      setWorkflowMessage("Workflow version created.");
      await loadWorkflows();
    } catch (submitError) {
      setWorkflowMessage(submitError.message);
    }
  };

  const loadDemoTemplate = () => {
    setWorkflow({
      slug: "openemr-provider-intake",
      name: "OpenEMR FHIR Provider Intake (Demo)",
      version: 1,
      agent_profile: profiles[0]?.profile_id || "",
      required_fields: [
        "practitioner.identifier",
        "practitioner.name.family",
        "practitioner.name.given",
        "practitioner.qualification.code",
      ].join("\n"),
      required_evidence_types: "license-document",
      require_evidence: true,
    });
    setRules([{
      ...blankRule(),
      key: "practitioner-identifier-required",
      field: "practitioner.identifier",
      message: "A practitioner identifier must be supplied.",
    }]);
    setSop({
      ...blankSop(),
      title: "Demo provider intake SOP (replace with approved client policy)",
      content: [
        "DEMO POLICY ONLY — this is not an official OpenEMR rule or clinical policy.",
        "For a normalized FHIR R4 Practitioner submission, verify that an identifier is present.",
        "Verify the practitioner's family name and at least one given name are present.",
        "Review the qualification code and confirm that supporting license documentation is attached.",
        "A human reviewer must verify the source and current license status before approval.",
      ].join("\n"),
      candidate_fields: [
        "practitioner.identifier",
        "practitioner.name.family",
        "practitioner.name.given",
        "practitioner.qualification.code",
      ].join("\n"),
    });
    setWorkflowMessage("Demo template loaded. Review it before creating the workflow.");
    setSopMessage("Demo template loaded. Replace the sample SOP with your approved instructions.");
  };

  const saveSop = async (event) => {
    event.preventDefault();
    setSopMessage("");
    try {
      if (!sopWorkflowId) throw new Error("Create and select a workflow before saving an SOP.");
      const saved = await api(`/workflows/${encodeURIComponent(sopWorkflowId)}/sops`, {
        method: "POST",
        body: JSON.stringify({
          title: sop.title.trim(),
          content: sop.content.trim(),
          confirm_no_phi_or_secrets: sop.confirm_no_phi_or_secrets,
        }),
      });
      await loadSops(sopWorkflowId);
      setSopId(saved.id);
      setSopMessage(`Saved SOP version ${saved.version}.`);
    } catch (saveError) {
      setSopMessage(saveError.message);
    }
  };

  const requestSuggestions = async () => {
    setSopMessage("");
    try {
      const candidateFields = lines(sop.candidate_fields);
      if (!sopId || !sopWorkflowId) throw new Error("Save an SOP version and select its workflow first.");
      if (!candidateFields.length) throw new Error("Enter at least one candidate field path.");
      const result = await api(`/workflows/${encodeURIComponent(sopWorkflowId)}/rule-suggestions`, {
        method: "POST",
        body: JSON.stringify({
          sop_id: sopId,
          candidate_fields: candidateFields,
          confirm_no_phi_or_secrets: sop.confirm_no_phi_or_secrets && sop.confirm_candidate_fields,
        }),
      });
      setSuggestionJobId(result.job_id);
      setSuggestionResult(null);
      setSelectedSuggestions([]);
      setSopMessage(`Suggestion job ${result.job_id.slice(0, 8)} is ${result.status}.`);
    } catch (requestError) {
      setSopMessage(requestError.message);
    }
  };

  const checkSuggestions = async () => {
    if (!suggestionJobId) return;
    try {
      const job = await api(`/agent-jobs/${encodeURIComponent(suggestionJobId)}`);
      setSopMessage(`Suggestion job status: ${job.status} · ${job.progress_message || ""}`);
      if (job.status === "succeeded" && job.result) {
        setSuggestionResult(job.result);
        setSelectedSuggestions([]);
      } else if (job.status === "failed") {
        setSuggestionResult({ summary: `Suggestion generation failed (${job.error_code || "unknown error"}).`, suggestions: [] });
      }
    } catch (checkError) {
      setSopMessage(checkError.message);
    }
  };

  const approveSuggestions = async () => {
    try {
      if (!selectedSuggestions.length) throw new Error("Select at least one suggested rule to approve.");
      const updated = await api(
        `/workflows/${encodeURIComponent(sopWorkflowId)}/rule-suggestions/${encodeURIComponent(suggestionJobId)}/approve`,
        { method: "POST", body: JSON.stringify({ rule_keys: selectedSuggestions }) },
      );
      setSopMessage(`Approved into workflow version ${updated.version}.`);
      await loadWorkflows();
    } catch (approveError) {
      setSopMessage(approveError.message);
    }
  };

  const openCase = async (caseId) => {
    setError("");
    setReviewMessage("");
    setCurrentCase({ loading: true });
    setReview({
      decision: "approve",
      note: "",
      confirm_qc_reviewed: false,
      confirm_evidence_reviewed: false,
    });
    try {
      const [detail, agentRuns, audit] = await Promise.all([
        api(`/cases/${encodeURIComponent(caseId)}`),
        api(`/cases/${encodeURIComponent(caseId)}/agent-runs?limit=20`),
        api(`/cases/${encodeURIComponent(caseId)}/audit`),
      ]);
      setCurrentCase(detail);
      setCaseExtras({ agentRuns, audit });
    } catch (loadError) {
      setCurrentCase(null);
      setError(loadError.message);
    }
  };

  const submitReview = async (event) => {
    event.preventDefault();
    if (!currentCase || currentCase.loading) return;
    setReviewMessage("");
    try {
      const result = await api(`/cases/${encodeURIComponent(currentCase.id)}/review`, {
        method: "POST",
        body: JSON.stringify(review),
      });
      setCurrentCase(result);
      setReviewMessage("Review decision recorded.");
      await loadCases(offset, caseStatus);
    } catch (reviewError) {
      setReviewMessage(reviewError.message);
    }
  };

  const changeStatus = async (value) => {
    setCaseStatus(value);
    setOffset(0);
    try {
      await loadCases(0, value);
    } catch (loadError) {
      setError(loadError.message);
    }
  };

  const casesByStatus = monitoring?.summary?.cases_by_status || {};
  const jobsByStatus = monitoring?.summary?.jobs_by_status || {};
  const casesTotal = Object.values(casesByStatus).reduce((sum, value) => sum + value, 0);
  const jobsTotal = Object.values(jobsByStatus).reduce((sum, value) => sum + value, 0);

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">QC PLATFORM</p>
          <h1>{tab === "monitoring" ? "Operations monitoring" : tab === "intake" ? "Cross-platform intake" : "Client workspace"}</h1>
          <a className="portal-link" href={tab === "monitoring" ? "/client-portal" : "/monitoring"}
            onClick={(event) => { event.preventDefault(); navigate(tab === "monitoring" ? "workflows" : "monitoring"); }}>
            {tab === "monitoring" ? "Open client workspace" : "Operations monitoring"}
          </a>
        </div>
        <form className="connection" onSubmit={connect}>
          <label htmlFor="api-key">Tenant reviewer/admin API key</label>
          <div className="connection-row">
            <input id="api-key" type="password" autoComplete="off" placeholder="Paste API key"
              value={apiKeyInput} onChange={(event) => setApiKeyInput(event.target.value)} />
            <button type="submit">{apiKey ? "Reconnect" : "Connect"}</button>
            {apiKey && <button className="secondary" type="button" onClick={disconnect}>Disconnect</button>}
          </div>
          <span id="connection-state" role="status">{connectionState}</span>
        </form>
      </header>
      <main>
        {error && <div className="error-banner" role="alert">{error}<button className="banner-close" onClick={() => setError("")}>Dismiss</button></div>}
        {tab !== "monitoring" ? (
          <>
            <nav className="tabs" aria-label="Client workspace">
              <button className={`tab${tab === "workflows" ? " active" : ""}`} onClick={() => navigate("workflows")}>QC workflows</button>
              <button className={`tab${tab === "cases" ? " active" : ""}`} onClick={() => navigate("cases")}>Case review</button>
              <button className={`tab${tab === "intake" ? " active" : ""}`} onClick={() => navigate("intake")}>Cross-platform intake</button>
              <button className="tab-link" onClick={() => navigate("monitoring")}>Operations monitoring</button>
            </nav>
            {tab === "workflows" ? (
              <WorkflowPanel
                apiKey={apiKey}
                workflows={workflows}
                profiles={profiles}
                isTenantAdmin={isTenantAdmin}
                onLoadDemo={loadDemoTemplate}
                workflow={workflow}
                setWorkflow={updateWorkflow}
                rules={rules}
                setRules={setRules}
                onSubmit={submitWorkflow}
                message={workflowMessage}
                sop={sop}
                setSop={setSop}
                sopWorkflowId={sopWorkflowId}
                setSopWorkflowId={setSopWorkflowId}
                sops={sops}
                sopId={sopId}
                setSopId={setSopId}
                sopMessage={sopMessage}
                onSaveSop={saveSop}
                onRequestSuggestions={requestSuggestions}
                suggestionJobId={suggestionJobId}
                onCheckSuggestions={checkSuggestions}
                suggestionResult={suggestionResult}
                selectedSuggestions={selectedSuggestions}
                setSelectedSuggestions={setSelectedSuggestions}
                onApproveSuggestions={approveSuggestions}
                onRefresh={() => loadWorkflows().catch((loadError) => setError(loadError.message))}
              />
            ) : tab === "cases" ? (
              <CasePanel
                cases={cases}
                status={caseStatus}
                onStatusChange={changeStatus}
                onRefresh={() => loadCases().catch((loadError) => setError(loadError.message))}
                onOpen={openCase}
                offset={offset}
                onPage={(next) => loadCases(next).catch((loadError) => setError(loadError.message))}
              />
            ) : (
              <IntakePanel
                apiKey={apiKey}
                workflows={availableIntakeWorkflows}
                workflowSlug={intakeWorkflowSlug}
                setWorkflowSlug={setIntakeWorkflowSlug}
                reference={intakeReference}
                setReference={setIntakeReference}
                sourceSystem={intakeSourceSystem}
                setSourceSystem={setIntakeSourceSystem}
                sources={intakeSources}
                updateSource={updateIntakeSource}
                updateMapping={updateIntakeMapping}
                addSource={addIntakeSource}
                removeSource={removeIntakeSource}
                addMapping={addIntakeMapping}
                removeMapping={removeIntakeMapping}
                confirmed={intakeConfirmed}
                setConfirmed={setIntakeConfirmed}
                message={intakeMessage}
                result={intakeCase}
                onSubmit={submitIntake}
              />
            )}
          </>
        ) : (
          <MonitoringPanel
            monitoring={monitoring}
            days={windowDays}
            setDays={setWindowDays}
            onRefresh={refreshMonitoring}
            lastUpdated={lastUpdated}
            casesTotal={casesTotal}
            jobsTotal={jobsTotal}
            jobsByStatus={jobsByStatus}
          />
        )}
      </main>
      {currentCase && (
        <CaseDialog
          item={currentCase}
          extras={caseExtras}
          review={review}
          setReview={setReview}
          message={reviewMessage}
          onSubmit={submitReview}
          onClose={() => setCurrentCase(null)}
        />
      )}
    </>
  );
}

function WorkflowPanel(props) {
  const {
    apiKey, workflows, profiles, isTenantAdmin, workflow, setWorkflow, rules, setRules, onSubmit, message,
    onLoadDemo,
    sop, setSop, sopWorkflowId, setSopWorkflowId, sops, sopId, setSopId, sopMessage, onSaveSop,
    onRequestSuggestions, suggestionJobId, onCheckSuggestions, suggestionResult, selectedSuggestions,
    setSelectedSuggestions, onApproveSuggestions, onRefresh,
  } = props;
  const canEdit = Boolean(apiKey && isTenantAdmin);
  return (
    <>
      <div className="toolbar">
        <div><h2>Configure QC workflows</h2><p>Create tenant-specific, versioned process configurations.</p></div>
        <div className="controls">
          {canEdit && <button className="secondary" onClick={onLoadDemo}>Load OpenEMR FHIR demo</button>}
          <button className="secondary" onClick={onRefresh}>Refresh workflows</button>
        </div>
      </div>
      {canEdit && (
        <section className="panel" aria-labelledby="workflow-editor-title">
          <div className="panel-heading"><div><h2 id="workflow-editor-title">New workflow version</h2><p>Define data checks, evidence requirements, and an approved model profile.</p></div></div>
          <form onSubmit={onSubmit}>
            <div className="form-grid">
              <Field label="Workflow slug"><input name="slug" required pattern="[a-z0-9][a-z0-9-]{1,98}[a-z0-9]" maxLength="100" placeholder="credentialing" value={workflow.slug} onChange={setWorkflow} /></Field>
              <Field label="Display name"><input name="name" required maxLength="200" value={workflow.name} onChange={setWorkflow} /></Field>
              <Field label="Version"><input name="version" type="number" min="1" step="1" required value={workflow.version} onChange={setWorkflow} /></Field>
              <Field label="Agent model profile"><select name="agent_profile" required value={workflow.agent_profile} onChange={setWorkflow}><option value="">Select a profile</option>{profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.profile_id} ({profile.provider} · {profile.model})</option>)}</select></Field>
              <Field className="wide" label="Required field paths" hint="One per line, e.g. provider.npi"><textarea name="required_fields" rows="3" value={workflow.required_fields} onChange={setWorkflow} /></Field>
              <Field className="wide" label="Required evidence types" hint="One per line, e.g. license-document"><textarea name="required_evidence_types" rows="3" value={workflow.required_evidence_types} onChange={setWorkflow} /></Field>
              <label className="checkbox-label wide"><input name="require_evidence" type="checkbox" checked={workflow.require_evidence} onChange={setWorkflow} />Require at least one evidence item</label>
            </div>
            <div className="rules-heading"><div><h3>Field rules</h3><p className="hint">Rules run deterministically when a case is submitted or revalidated.</p></div><button className="secondary" type="button" onClick={() => setRules([...rules, blankRule()])}>Add field rule</button></div>
            <div className="rule-list">
              {rules.map((rule, index) => (
                <div className="rule-card" key={index}>
                  <RuleInput label="Rule key" name="key" value={rule.key} onChange={(value) => updateRule(setRules, rules, index, "key", value)} />
                  <RuleInput label="Field path" name="field" value={rule.field} onChange={(value) => updateRule(setRules, rules, index, "field", value)} />
                  <label>Check<select value={rule.operator} onChange={(event) => updateRule(setRules, rules, index, "operator", event.target.value)}>{OPERATORS.map((operator) => <option key={operator}>{operator}</option>)}</select></label>
                  <label className="rule-value">{rule.operator === "equals_field" ? "Compare with field path" : "Value"}<input value={rule.value} disabled={rule.operator === "required"} required={rule.operator !== "required"} onChange={(event) => updateRule(setRules, rules, index, "value", event.target.value)} placeholder={rule.operator === "required" ? "No value required" : rule.operator === "equals_field" ? "payer.provider.npi" : 'e.g. ^[0-9]{10}$ or ["CA","NY"]'} /></label>
                  <label>Severity<select value={rule.severity} onChange={(event) => updateRule(setRules, rules, index, "severity", event.target.value)}><option value="error">error</option><option value="warning">warning</option></select></label>
                  <RuleInput className="rule-message" label="Reviewer message" value={rule.message} onChange={(value) => updateRule(setRules, rules, index, "message", value)} />
                  <button className="remove-rule" type="button" onClick={() => setRules(rules.filter((_, itemIndex) => itemIndex !== index))}>Remove</button>
                </div>
              ))}
            </div>
            <div className="form-actions"><button type="submit">Create workflow version</button><span role="status">{message}</span></div>
          </form>
        </section>
      )}
      {apiKey && !isTenantAdmin && <p className="notice">Workflow configuration and SOP suggestions require a tenant-admin API key. Case review is available to reviewers and tenant admins.</p>}
      {canEdit && (
        <section className="panel">
          <div className="panel-heading"><div><h2>SOP-guided agent rule suggestions</h2><p>Save approved process instructions, request draft rules, then approve selected rules into a new version.</p></div></div>
          <p className="notice">Only SOP text and field names are sent to the configured model. Do not enter PHI, personal data, credentials, or secrets. Suggestions are never activated automatically.</p>
          <form onSubmit={onSaveSop}>
            <div className="form-grid">
              <Field label="QC workflow"><select value={sopWorkflowId} onChange={(event) => setSopWorkflowId(event.target.value)}><option value="">Select a workflow</option>{workflows.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.slug} · v{item.version}</option>)}</select></Field>
              <Field label="Saved SOP version"><select value={sopId} onChange={(event) => setSopId(event.target.value)}><option value="">Select an SOP</option>{sops.map((item) => <option key={item.id} value={item.id}>{item.title} · v{item.version}</option>)}</select></Field>
              <Field className="wide" label="SOP title"><input required maxLength="200" value={sop.title} onChange={(event) => setSop({ ...sop, title: event.target.value })} /></Field>
              <Field className="wide" label="SOP text"><textarea required rows="7" maxLength="20000" value={sop.content} onChange={(event) => setSop({ ...sop, content: event.target.value })} /></Field>
              <label className="checkbox-label wide"><input type="checkbox" required checked={sop.confirm_no_phi_or_secrets} onChange={(event) => setSop({ ...sop, confirm_no_phi_or_secrets: event.target.checked })} />I confirm this SOP contains no PHI, personal identifiers, credentials, or secrets.</label>
              <Field className="wide" label="Candidate field paths" hint="Only these schema names may be referenced by suggested rules; one per line."><textarea required rows="4" value={sop.candidate_fields} onChange={(event) => setSop({ ...sop, candidate_fields: event.target.value })} /></Field>
              <label className="checkbox-label wide"><input type="checkbox" required checked={sop.confirm_candidate_fields} onChange={(event) => setSop({ ...sop, confirm_candidate_fields: event.target.checked })} />I confirm these are field paths/schema names only and contain no real case values.</label>
            </div>
            <div className="form-actions"><button type="submit">Save SOP version</button><button type="button" className="secondary" disabled={!sopId} onClick={onRequestSuggestions}>Request agent suggestions</button><button type="button" className="secondary" disabled={!suggestionJobId} onClick={onCheckSuggestions}>Check suggestion job</button><span role="status">{sopMessage}</span></div>
          </form>
          {suggestionResult && (
            <div className="suggestion-results">
              <h3>{suggestionResult.summary}</h3>
              {suggestionResult.suggestions?.map((suggestion) => (
                <article className="suggestion-card" key={suggestion.rule.key}>
                  <input type="checkbox" aria-label={`Select ${suggestion.rule.key}`} checked={selectedSuggestions.includes(suggestion.rule.key)}
                    onChange={(event) => setSelectedSuggestions(event.target.checked ? [...selectedSuggestions, suggestion.rule.key] : selectedSuggestions.filter((key) => key !== suggestion.rule.key))} />
                  <div><h3>{suggestion.rule.key} · {suggestion.rule.field} · {suggestion.rule.operator}</h3><p>Rule: {JSON.stringify(suggestion.rule)}</p><p>Rationale: {suggestion.rationale}</p><p>SOP excerpt: “{suggestion.sop_excerpt}”</p></div>
                </article>
              ))}
              {suggestionResult.suggestions?.length > 0 && <div className="form-actions"><button onClick={onApproveSuggestions}>Approve selected rules into a new workflow version</button></div>}
            </div>
          )}
        </section>
      )}
      <section className="panel">
        <div className="panel-heading"><div><h2>Configured workflows</h2><p>Workflow definitions are isolated to the connected tenant.</p></div><span>{workflows.length} configured</span></div>
        <div className="table-wrap"><table><thead><tr><th>Name</th><th>Slug</th><th>Version</th><th>Status</th><th>Rules</th><th>Evidence</th><th>Model</th></tr></thead><tbody>
          {workflows.length ? workflows.map((item) => <tr key={item.id}><td>{item.name}</td><td>{item.slug}</td><td>{item.version}</td><td><span className={`workflow-status${item.active ? "" : " inactive"}`}>{item.active ? "Active" : "Inactive"}</span></td><td>{item.required_fields.length + item.field_rules.length}</td><td>{item.required_evidence_types.join(", ") || (item.require_evidence ? "At least one" : "None")}</td><td>{item.agent_profile}</td></tr>) : <EmptyRow colSpan={7} message={apiKey ? "No workflows yet." : "Connect to load workflows."} />}
        </tbody></table></div>
      </section>
    </>
  );
}

function RuleInput({ label, value, onChange, className = "" }) {
  return <label className={className}>{label}<input required maxLength="300" value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}

function IntakePanel({
  apiKey,
  workflows,
  workflowSlug,
  setWorkflowSlug,
  reference,
  setReference,
  sourceSystem,
  setSourceSystem,
  sources,
  updateSource,
  updateMapping,
  addSource,
  removeSource,
  addMapping,
  removeMapping,
  confirmed,
  setConfirmed,
  message,
  result,
  onSubmit,
}) {
  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Cross-platform document intake</h2>
          <p>Choose a QC workflow, add the records you want to compare, and map their fields.</p>
        </div>
      </div>
      <section className="panel">
        <p className="notice">
          Synthetic/test data only. Files are parsed in memory and not retained; mapped values become case data
          and evidence records keep a content hash, not the original document. Supported: JSON, mapped FHIR JSON
          Bundles, text-based PDFs, and XLSX. Scanned-PDF OCR, DOCX, HL7 v2, live payer/client connections, and
          secure document storage are not included. Do not upload PHI or real customer documents.
        </p>
        <form onSubmit={onSubmit}>
          <div className="intake-steps">
            <section className="intake-step">
              <div className="intake-step-heading"><span>1</span><div><h3>Case details</h3><p>Choose the rules to run on the combined records.</p></div></div>
              <div className="form-grid">
                <Field className="wide" label="QC workflow" hint={workflows.length ? "The selected workflow will run after the files are mapped." : "Create a workflow first, then return here."}>
                  <select required value={workflowSlug} onChange={(event) => setWorkflowSlug(event.target.value)}>
                    <option value="">Select a workflow</option>
                    {workflows.map((item) => <option value={item.slug} key={item.id}>{item.name} · {item.slug} · v{item.version}</option>)}
                  </select>
                </Field>
                <Field label="Case reference (optional)" hint="For example, a synthetic provider or enrollment ID.">
                  <input maxLength="160" value={reference} onChange={(event) => setReference(event.target.value)} placeholder="synthetic-provider-001" />
                </Field>
                <Field label="Intake source" hint="A short name for this submission batch.">
                  <input required maxLength="100" value={sourceSystem} onChange={(event) => setSourceSystem(event.target.value)} />
                </Field>
              </div>
            </section>
            <section className="intake-step">
              <div className="intake-step-heading"><span>2</span><div><h3>Add source files</h3><p>Add at least two records to compare. One file represents one record.</p></div></div>
              <div className="intake-source-list">
                {sources.map((source, index) => {
                  const locatorLabel = source.format === "json" || source.format === "fhir_json"
                    ? "JSON Pointer"
                    : source.format === "pdf" ? "PDF text pattern" : "Spreadsheet column";
                  const locatorPlaceholder = source.format === "json" ? "/provider/npi"
                    : source.format === "fhir_json" ? "/entry/0/resource/identifier/0/value"
                      : source.format === "pdf" ? "NPI:\\s*([0-9]+)" : "Provider NPI";
                  const locatorHint = source.format === "json" || source.format === "fhir_json"
                    ? "Use a JSON Pointer, such as /provider/npi."
                    : source.format === "pdf" ? "Use one text pattern with a capture group, such as NPI:\\s*([0-9]+)."
                      : "Enter the exact header text from the first row.";
                  return (
                    <article className="intake-source-card" key={source.id}>
                      <div className="intake-source-heading">
                        <div><span className="eyebrow">SOURCE {index + 1}</span><h4>{source.source_id || "Name this source"}</h4></div>
                        {sources.length > 2 && <button className="text-button" type="button" onClick={() => removeSource(source.id)}>Remove source</button>}
                      </div>
                      <div className="form-grid">
                        <Field label="Source name" hint="Unique ID used to identify this record in the case.">
                          <input required maxLength="100" pattern="[A-Za-z][A-Za-z0-9_.-]{0,99}" title="Start with a letter; use only letters, numbers, periods, underscores, or hyphens." value={source.source_id} onChange={(event) => updateSource(source.id, { source_id: event.target.value })} placeholder="client_platform" />
                        </Field>
                        <Field label="File format">
                          <select value={source.format} onChange={(event) => updateSource(source.id, { format: event.target.value, file: null })}>
                            <option value="json">JSON</option>
                            <option value="fhir_json">FHIR JSON Bundle</option>
                            <option value="pdf">Text-based PDF</option>
                            <option value="xlsx">Excel workbook (XLSX)</option>
                          </select>
                        </Field>
                        <Field className="wide" label="Choose file" hint="Maximum 768 KB per file; 850 KB combined across this form.">
                          <input
                            key={`${source.id}-${source.format}-${source.file?.name || "empty"}`}
                            required
                            type="file"
                            accept={source.format === "pdf" ? ".pdf,application/pdf" : source.format === "xlsx" ? ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : ".json,application/json"}
                            onChange={(event) => updateSource(source.id, { file: event.target.files?.[0] || null })}
                          />
                          {source.file && <span className="selected-file-row"><span className="selected-file">{source.file.name} · {(source.file.size / 1024).toFixed(1)} KB</span><button type="button" className="text-button" onClick={() => updateSource(source.id, { file: null })}>Remove file</button></span>}
                        </Field>
                        <Field label="Evidence category" hint="For example, client-record or payer-roster.">
                          <input required maxLength="100" value={source.evidence_type} onChange={(event) => updateSource(source.id, { evidence_type: event.target.value })} />
                        </Field>
                        {source.format === "xlsx" && <>
                          <Field label="Worksheet (optional)" hint="Leave blank to use the first worksheet.">
                            <input maxLength="100" value={source.worksheet} onChange={(event) => updateSource(source.id, { worksheet: event.target.value })} placeholder="Sheet1" />
                          </Field>
                          <Field label="Data row" hint="Row number after the header row.">
                            <input required type="number" min="1" max="10001" value={source.row_number} onChange={(event) => updateSource(source.id, { row_number: event.target.value })} />
                          </Field>
                        </>}
                      </div>
                      <div className="mapping-heading"><div><h4>Field mappings</h4><p>Tell QC which source values fill the case fields.</p></div></div>
                      <div className="mapping-list">
                        {source.mappings.map((mapping, mappingIndex) => (
                          <div className="mapping-row" key={mapping.id}>
                            <Field label={mappingIndex === 0 ? "Case field" : `Case field ${mappingIndex + 1}`} hint={mappingIndex === 0 ? "Example: client.provider.npi" : undefined}>
                              <input required maxLength="100" value={mapping.target} onChange={(event) => updateMapping(source.id, mapping.id, "target", event.target.value)} placeholder="client.provider.npi" />
                            </Field>
                            <Field label={locatorLabel} hint={mappingIndex === 0 ? locatorHint : undefined}>
                              <input required maxLength="512" value={mapping.locator} onChange={(event) => updateMapping(source.id, mapping.id, "locator", event.target.value)} placeholder={locatorPlaceholder} />
                            </Field>
                            {source.mappings.length > 1 && <button type="button" className="text-button mapping-remove" onClick={() => removeMapping(source.id, mapping.id)} aria-label={`Remove mapping ${mappingIndex + 1}`}>Remove</button>}
                          </div>
                        ))}
                      </div>
                      {source.mappings.length < 100 && <button className="secondary add-mapping" type="button" onClick={() => addMapping(source.id)}>Add another field</button>}
                    </article>
                  );
                })}
              </div>
              {sources.length < 20 && <button className="secondary" type="button" onClick={addSource}>Add another source</button>}
              <p className="hint intake-total">{sources.reduce((sum, source) => sum + (source.file?.size || 0), 0).toLocaleString()} of 850 KB total selected</p>
            </section>
            <section className="intake-step">
              <div className="intake-step-heading"><span>3</span><div><h3>Confirm and run QC</h3><p>Review the mapping and create a case using the selected workflow.</p></div></div>
              <label className="checkbox-label synthetic-confirmation">
                <input type="checkbox" required checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
                I confirm these uploads contain synthetic/test data only and no PHI or real customer documents.
              </label>
            </section>
          </div>
          <div className="form-actions">
            <button type="submit" disabled={!apiKey || !workflows.length}>Create case and run QC</button>
            <span role="status">{message}</span>
          </div>
        </form>
      </section>
      {result && (
        <section className="panel">
          <div className="panel-heading">
            <div><h2>Intake QC result</h2><p>Case {result.id} · {result.status}</p></div>
          </div>
          <div className="table-wrap">
            <table><thead><tr><th>Check</th><th>Status</th><th>Field</th><th>Explanation</th></tr></thead><tbody>
              {result.checks.map((check) => (
                <tr key={check.id}>
                  <td>{check.rule_key}</td>
                  <td><span className={`pill ${check.status}`}>{check.status}</span></td>
                  <td>{check.result.field || check.result.evidence_type || "—"}</td>
                  <td>{check.result.message || (check.result.compared_field ? `Compared with ${check.result.compared_field}.` : "Check passed.")}</td>
                </tr>
              ))}
            </tbody></table>
          </div>
          <div className="detail-section">
            <h3>Uploaded source provenance</h3>
            <div className="detail-list">
              {result.evidence.map((evidence) => (
                <article className="detail-item" key={evidence.id}>
                  <strong>{evidence.source_system} · {evidence.attributes.filename}</strong>
                  <p>{evidence.attributes.format} · SHA-256 {evidence.sha256} · {evidence.attributes.extracted_field_count} mapped fields</p>
                </article>
              ))}
            </div>
          </div>
        </section>
      )}
    </>
  );
}

function updateRule(setRules, rules, index, key, value) {
  setRules(rules.map((rule, ruleIndex) => ruleIndex === index ? { ...rule, [key]: value } : rule));
}

function Field({ label, hint, className = "", children }) {
  return <label className={className}>{label}{hint && <span className="hint">{hint}</span>}{children}</label>;
}

function CasePanel({ cases, status, onStatusChange, onRefresh, onOpen, offset, onPage }) {
  return (
    <>
      <div className="toolbar"><div><h2>Human case review</h2><p>Inspect submitted data, evidence, QC findings, agent recommendations, and review history.</p></div>
        <div className="controls"><label htmlFor="case-status">Status</label><select id="case-status" value={status} onChange={(event) => onStatusChange(event.target.value)}><option value="">All statuses</option><option value="in_review">In review</option><option value="needs_information">Needs information</option><option value="approved">Approved</option><option value="rejected">Rejected</option></select><button className="secondary" onClick={onRefresh}>Refresh cases</button></div>
      </div>
      <section className="panel"><div className="panel-heading"><div><h2>Submitted cases</h2><p>Review cases using your reviewer or administrator key.</p></div><span>{cases.length} shown on this page</span></div>
        <div className="table-wrap"><table><thead><tr><th>Reference</th><th>Source</th><th>QC workflow</th><th>Status</th><th>Submitted</th><th></th></tr></thead><tbody>
          {cases.length ? cases.map((item) => <tr key={item.id}><td>{item.external_reference || item.id.slice(0, 8)}</td><td>{item.source_system}</td><td>{item.workflow_slug} · v{item.workflow_version}</td><td><span className={`pill ${item.status}`}>{item.status.replaceAll("_", " ")}</span></td><td>{formatDate(item.created_at)}</td><td><button className="case-open" onClick={() => onOpen(item.id)}>Review</button></td></tr>) : <EmptyRow colSpan={6} message="No cases match this status on this page." />}
        </tbody></table></div>
        <div className="pagination"><button className="secondary" disabled={!offset} onClick={() => onPage(Math.max(0, offset - PAGE_SIZE))}>Previous</button><span>Page {Math.floor(offset / PAGE_SIZE) + 1}</span><button className="secondary" disabled={cases.length < PAGE_SIZE} onClick={() => onPage(offset + PAGE_SIZE)}>Next</button></div>
      </section>
    </>
  );
}

function CaseDialog({ item, extras, review, setReview, message, onSubmit, onClose }) {
  if (!item) return null;
  if (item.loading) return <div className="dialog-scrim"><section className="case-dialog panel"><p>Loading case…</p><button className="secondary" onClick={onClose}>Close</button></section></div>;
  const finalDecision = ["approved", "rejected"].includes(item.status);
  const setField = (event) => setReview({ ...review, [event.target.name]: event.target.type === "checkbox" ? event.target.checked : event.target.value });
  return (
    <div className="dialog-scrim" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="case-dialog panel" role="dialog" aria-modal="true" aria-labelledby="case-title">
        <div className="dialog-heading"><div><p className="eyebrow">CASE REVIEW</p><h2 id="case-title">{item.external_reference || `Case ${item.id.slice(0, 8)}`}</h2></div><button className="secondary" onClick={onClose}>Close</button></div>
        <section className="detail-section detail-grid">{[["Workflow", `${item.workflow_slug} · v${item.workflow_version}`], ["Status", item.status.replaceAll("_", " ")], ["Source system", item.source_system], ["Submitted", formatDate(item.created_at)]].map(([label, value]) => <div key={label}><strong>{label}</strong><span>{value}</span></div>)}</section>
        <DetailSection title="Submitted application data" value={item.data} />
        <DetailList title={`QC checks (${item.checks.length})`} items={item.checks} empty="No check results are available." render={(check) => [`${check.rule_key} · ${check.status}`, check.result]} />
        <DetailList title={`Evidence (${item.evidence.length})`} items={item.evidence} empty="No evidence attached." render={(evidence) => [`${evidence.evidence_type} · ${evidence.source_system}`, { reference: evidence.reference, sha256: evidence.sha256, attributes: evidence.attributes }]} />
        <DetailList title={`Agent recommendations (${extras.agentRuns.length})`} items={extras.agentRuns} empty="No agent recommendation has been generated." render={(run) => [`${run.provider} · ${run.model} · ${formatDate(run.created_at)}`, run.recommendation]} />
        <DetailList title={`Audit history (${extras.audit.length})`} items={extras.audit} empty="No audit events available." render={(event) => [`${event.action} · ${formatDate(event.occurred_at)}`, { actor_role: event.actor_role, details: event.details }]} />
        {finalDecision ? <p className="decision-readonly">This case has a final decision ({item.status}) and cannot be reviewed again.</p> : (
          <form className="review-form" onSubmit={onSubmit}>
            <h3>Human decision</h3>
            <Field label="Decision"><select name="decision" value={review.decision} onChange={setField}><option value="approve">Approve</option><option value="reject">Reject</option><option value="request_information">Request information</option></select></Field>
            {review.decision === "approve" && <fieldset className="approval-attestations"><legend>Approval attestations</legend>
              <label className="attestation"><input type="checkbox" name="confirm_qc_reviewed" required checked={review.confirm_qc_reviewed} onChange={setField} />I reviewed the latest QC checks and resolved any warnings appropriately.</label>
              <label className="attestation"><input type="checkbox" name="confirm_evidence_reviewed" required checked={review.confirm_evidence_reviewed} onChange={setField} />I reviewed the evidence and its source for this case.</label>
            </fieldset>}
            <Field label="Review note"><textarea name="note" rows="3" maxLength="2000" required value={review.note} onChange={setField} placeholder="Document the reason for this decision" /></Field>
            <div className="form-actions"><button type="submit">Submit review decision</button><span role="status">{message}</span></div>
          </form>
        )}
      </section>
    </div>
  );
}

function DetailSection({ title, value }) {
  return <section className="detail-section"><h3>{title}</h3><pre>{JSON.stringify(value, null, 2)}</pre></section>;
}

function DetailList({ title, items, render, empty }) {
  return <section className="detail-section"><h3>{title}</h3>{items.length ? <div className="detail-list">{items.map((item, index) => {
    const [heading, value] = render(item);
    return <article className="detail-item" key={item.id || index}><strong>{heading}</strong><p>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</p></article>;
  })}</div> : <p className="hint">{empty}</p>}</section>;
}

function MonitoringPanel({ monitoring, days, setDays, onRefresh, lastUpdated, casesTotal, jobsTotal, jobsByStatus }) {
  const dailyCases = monitoring?.summary?.daily_cases || [];
  const maxCases = Math.max(1, ...dailyCases.map((point) => point.count));
  return (
    <>
      <section className="toolbar"><div><h2>Service overview</h2><p>Tenant-scoped workflow and background-job activity</p></div><div className="controls"><label htmlFor="window">Time window</label><select id="window" value={days} onChange={(event) => setDays(event.target.value)}>{["7", "14", "30", "90"].map((item) => <option key={item} value={item}>{item} days</option>)}</select><button className="secondary" onClick={onRefresh}>Refresh</button><span>{lastUpdated ? `Updated ${lastUpdated}` : "—"}</span></div></section>
      <section className="cards" aria-label="Key metrics">
        <Metric title="Total cases" value={monitoring ? casesTotal : "—"} note="All statuses" />
        <Metric title="Jobs in window" value={monitoring ? jobsTotal : "—"} note={`${jobsByStatus.succeeded || 0} completed · ${jobsByStatus.failed || 0} failed`} />
        <Metric title="Active jobs" value={monitoring ? (jobsByStatus.queued || 0) + (jobsByStatus.running || 0) : "—"} note="Queued or running" />
        <Metric title="Job errors" value={monitoring ? monitoring.summary.error_count : "—"} note="Failed attempts in window" alert />
      </section>
      <section className="charts">
        <article className="panel"><div className="panel-heading"><div><h2>Cases over time</h2><p>Created per day</p></div></div>
          {dailyCases.length ? <div className="react-chart">{dailyCases.map((point) => <div className="chart-column" key={point.date} title={`${point.date}: ${point.count}`}><strong>{point.count}</strong><span style={{ height: `${Math.max(3, (point.count / maxCases) * 100)}%` }} /><small>{point.date.slice(5)}</small></div>)}</div> : <p className="empty">Connect to load case activity.</p>}
        </article>
        <article className="panel"><div className="panel-heading"><div><h2>Automation jobs</h2><p>Job lifecycle status</p></div></div><div className="status-list">
          {STATUSES.map((status) => { const count = jobsByStatus[status] || 0; const total = jobsTotal || 1; return <div className={`status-row status-${status}`} key={status}><span>{status}</span><div className="status-track"><div className="status-fill" style={{ width: `${(count / total) * 100}%` }} /></div><strong>{count}</strong></div>; })}
        </div></article>
      </section>
      <section className="panel"><div className="panel-heading"><div><h2>Recent automation jobs</h2><p>Track progress, retries, and outcomes</p></div><span>{monitoring?.jobs.length || 0} shown</span></div><div className="table-wrap"><table><thead><tr><th>Job ID</th><th>Type</th><th>Status</th><th>Progress</th><th>Attempts</th><th>Updated</th><th>Error code</th></tr></thead><tbody>
        {monitoring?.jobs.length ? monitoring.jobs.map((job) => <tr key={job.id}><td>{job.id.slice(0, 8)}</td><td>{job.job_type}</td><td><span className={`pill ${job.status}`}>{job.status}</span></td><td>{job.progress}% · {job.progress_message}</td><td>{job.attempts}/{job.max_attempts}</td><td>{formatDate(job.updated_at)}</td><td>{job.error_code || "—"}</td></tr>) : <EmptyRow colSpan={7} message="Connect to load job status." />}
      </tbody></table></div></section>
      <section className="panel"><div className="panel-heading"><div><h2>Error and retry log</h2><p>Redacted operational failures; no case data or stack traces</p></div></div><div className="table-wrap"><table><thead><tr><th>Started</th><th>Job</th><th>Attempt</th><th>Outcome</th><th>Error code</th><th>Duration</th></tr></thead><tbody>
        {monitoring?.errors.length ? monitoring.errors.map((attempt) => <tr key={attempt.id}><td>{formatDate(attempt.started_at)}</td><td>{attempt.job_id.slice(0, 8)}</td><td>{attempt.attempt_number}</td><td><span className="pill failed">{attempt.status}</span></td><td>{attempt.error_code || "—"}</td><td>{attempt.duration_ms == null ? "—" : `${attempt.duration_ms} ms`}</td></tr>) : <EmptyRow colSpan={6} message="No error events loaded." />}
      </tbody></table></div></section>
    </>
  );
}

function Metric({ title, value, note, alert = false }) {
  return <article className={`card${alert ? " alert" : ""}`}><span>{title}</span><strong>{value}</strong><small>{note}</small></article>;
}

function EmptyRow({ colSpan, message }) {
  return <tr><td colSpan={colSpan} className="empty">{message}</td></tr>;
}

export default App;

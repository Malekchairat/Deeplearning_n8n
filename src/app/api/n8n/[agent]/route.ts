import { NextRequest, NextResponse } from "next/server";
import { simulationReply, webhookPaths } from "@/lib/solvarisk";

function describeBody(body: FormData | Record<string, unknown>, isForm: boolean) {
  if (isForm) {
    return Array.from((body as FormData).entries()).map(([key, value]) => [key, typeof value === "string" ? value : `[file: ${value.name}]`]);
  }
  return body;
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ agent: string }> }) {
  const { agent } = await params;
  const isForm = request.headers.get("content-type")?.includes("multipart/form-data");
  const body = isForm ? await request.formData() : await request.json();
  const requestBody = typeof body === "object" && body !== null ? body as Record<string, unknown> : {};
  const printableBody = describeBody(body, Boolean(isForm));
  // The RAG copilot and the multi-agent pipeline (LangGraph: data quality gate + Gemini risk analyst) run on the local
  // Python service (rag_service/); the other agents stay on n8n.
  const PYTHON_PATHS: Record<string, string> = { "rag-agent": "/ask", pipeline: "/api/pipeline/audit-and-risk", "qrt-extract": "/api/extract/qrt-pdf", "eiopa-agent": "/api/eiopa-agent/run", "climate-agent": "/api/climate-agent/run" };
  const useRagService = agent in PYTHON_PATHS;
  const baseUrl = useRagService ? (process.env.RAG_SERVICE_URL ?? "http://127.0.0.1:8001") : process.env.N8N_BASE_URL;
  const path = useRagService ? PYTHON_PATHS[agent] : webhookPaths[agent] ?? `/webhook/${agent}`;
  const targetUrl = `${baseUrl ?? ""}${path}`;
  const label = useRagService ? "rag" : "n8n";
  const isRiskAgent = agent === "risk-agent"; // http://localhost:5678/webhook/risk-agent via N8N_BASE_URL
  const failFast = isRiskAgent || agent === "pipeline" || agent === "notify" || agent === "qrt-extract" || agent === "eiopa-agent" || agent === "climate-agent"; // a made-up diagnosis (or a fake "e-mail sent") would be misleading: errors go to the UI as errors

  // The audit PDF is rendered by Python (rag_service/report_generator.py) and streamed back as binary.
  // Route 1: the n8n workflow (POST /webhook/report-pdf -> Python -> Respond to Webhook).
  // Route 2: straight to the Python service, used when n8n is off, the workflow is inactive or it fails.
  // Set REPORT_VIA_N8N=false to skip n8n. No LLM involved, so the simulation flag is ignored.
  if (agent === "report-pdf") {
    const routes: { name: "n8n" | "python"; url: string }[] = [];
    if (process.env.REPORT_VIA_N8N !== "false" && process.env.N8N_BASE_URL) routes.push({ name: "n8n", url: `${process.env.N8N_BASE_URL}${webhookPaths["report-pdf"]}` });
    routes.push({ name: "python", url: `${process.env.RAG_SERVICE_URL ?? "http://127.0.0.1:8001"}/report` });
    const failures: string[] = [];
    for (const route of routes) {
      try {
        const upstream = await fetch(route.url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(60000) });
        if (!upstream.ok) throw new Error(`HTTP ${upstream.status}: ${(await upstream.text()).slice(0, 200)}`);
        const bytes = await upstream.arrayBuffer();
        if (new TextDecoder().decode(bytes.slice(0, 5)) !== "%PDF-") throw new Error("la réponse n'est pas un PDF");
        console.info(`[SolvaRisk:${route.name}] report-pdf ok via ${route.name}`, { url: route.url, bytes: bytes.byteLength });
        return new Response(bytes, { headers: { "Content-Type": "application/pdf", "x-solvarisk-mode": "live", "x-solvarisk-route": route.name } });
      } catch (error) {
        const message = `${route.name}: ${error instanceof Error ? error.message : String(error)}`;
        console.error(`[SolvaRisk:${route.name}] report-pdf failed`, { url: route.url, error: message });
        failures.push(message);
      }
    }
    return NextResponse.json({ status: "error", error: failures.join(" | ") }, { status: 502, headers: { "x-solvarisk-mode": "error" } });
  }

  if (!baseUrl || (agent !== "pipeline" && agent !== "notify" && agent !== "qrt-extract" && agent !== "eiopa-agent" && agent !== "climate-agent" && (process.env.NEXT_PUBLIC_SIMULATION === "true" || requestBody.simulation === true))) {
    console.info(`[SolvaRisk:${label}] ${agent} simulation`, { targetUrl, payload: printableBody });
    return NextResponse.json(simulationReply(agent, requestBody));
  }

  console.info(`[SolvaRisk:${label}] ${agent} request`,{ url: targetUrl, payload: printableBody });

  try {
    const upstream = await fetch(targetUrl, {
      method: "POST",
      headers: isForm ? undefined : { "Content-Type": "application/json" },
      body: isForm ? body : JSON.stringify(body),
      signal: AbortSignal.timeout(useRagService ? 120000 : isRiskAgent ? 60000 : 8000), // RAG / pipeline: Gemini calls with retries and a fallback model; risk-agent: LLM analysis
    });

    const rawResponse = await upstream.text();
    console.info(`[SolvaRisk:${label}] ${agent} response`, { status: upstream.status, body: rawResponse });

    if (!upstream.ok) {
      throw new Error(`${label} a répondu HTTP ${upstream.status}: ${rawResponse || "corps vide"}`);
    }

    let parsedResponse: unknown;
    try {
      parsedResponse = JSON.parse(rawResponse);
    } catch {
      throw new Error(`Réponse ${label} non-JSON: ${rawResponse || "corps vide"}`);
    }

    if (agent === "rag-agent") {
      const answer = parsedResponse && typeof parsedResponse === "object" && "answer" in parsedResponse
        ? (parsedResponse as { answer?: unknown }).answer
        : undefined;
      if (typeof answer !== "string" || !answer.trim()) {
        throw new Error(`Réponse rag-agent invalide: la clé "answer" est absente ou vide. Payload reçu: ${rawResponse}`);
      }
    }

    return NextResponse.json(parsedResponse, { headers: { "x-solvarisk-mode": "live" } });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (failFast) {
      // A made-up diagnostic would be misleading: let the UI show its graceful error state instead.
      console.error(`[SolvaRisk:${label}] ${agent} failed`, { url: targetUrl, error: message });
      return NextResponse.json({ status: "error", error: message }, { status: 502, headers: { "x-solvarisk-mode": "error" } });
    }
    console.error(`[SolvaRisk:${label}] ${agent} failed; fallback simulation used`, { url: targetUrl, payload: printableBody, error: message });
    return NextResponse.json({ ...simulationReply(agent, requestBody), status: "simulation-fallback", error: message }, { headers: { "x-solvarisk-mode": "fallback" } });
  }
}

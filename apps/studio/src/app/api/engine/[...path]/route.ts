import { forwardEngineRequest } from "@/lib/engine-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type EngineRouteContext = {
  params: Promise<{ path: string[] }>;
};

async function forward(request: Request, context: EngineRouteContext): Promise<Response> {
  const { path } = await context.params;
  return forwardEngineRequest(request, path);
}

export function GET(request: Request, context: EngineRouteContext): Promise<Response> {
  return forward(request, context);
}

export function POST(request: Request, context: EngineRouteContext): Promise<Response> {
  return forward(request, context);
}

export function PUT(request: Request, context: EngineRouteContext): Promise<Response> {
  return forward(request, context);
}

export function PATCH(request: Request, context: EngineRouteContext): Promise<Response> {
  return forward(request, context);
}

export function DELETE(request: Request, context: EngineRouteContext): Promise<Response> {
  return forward(request, context);
}

export function HEAD(request: Request, context: EngineRouteContext): Promise<Response> {
  return forward(request, context);
}

export function OPTIONS(request: Request, context: EngineRouteContext): Promise<Response> {
  return forward(request, context);
}

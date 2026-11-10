"""Administrative HTTP surface: a human bearer identity and an access event per call."""

import logging

from starlette.responses import JSONResponse
from starlette.routing import Route

from vecten_mcp.admin_service import READS, WRITES, AdminRefused, AdminService, OperationRefused
from vecten_mcp.administration import AdoptionRequired
from vecten_mcp.failures import describe_error, warehouse_reason

__all__ = ["READS", "WRITES", "admin_routes"]
logger = logging.getLogger(__name__)


def failure(message, status, data=None):
    body = {"ok": False, "error": message}
    if data is not None:
        body["data"] = data
    return JSONResponse(body, status_code=status)


def admin_routes(runtime):
    auth = runtime.oauth
    service = getattr(runtime, "admin_service", None) or AdminService(runtime)

    async def endpoint(request):
        operation = request.path_params["operation"]
        principal_id, outcome = None, "denied"
        # Once the service runs it records the call; refusals before it are recorded here.
        recorded = False
        try:
            header = request.headers.get("authorization", "")
            if not header.startswith("Bearer "):
                return failure("vecten login is required", 401)
            try:
                caller = auth.resolve(header[7:])
            except ValueError:
                return failure("invalid access token", 401)
            principal_id, outcome = caller.principal["id"], "error"
            if int(request.headers.get("content-length", "0")) > 65536:
                return failure("request too large", 413)
            write = request.method == "POST"
            data = await request.json() if write else dict(request.query_params)
            if not isinstance(data, dict):
                return failure("invalid operation or request", 400)
            recorded = True
            result = service.call(caller, operation, data, surface="api", write=write)
            return JSONResponse({"ok": True, "data": result}, headers={"Cache-Control": "no-store"})
        except AdminRefused as exc:
            return failure(str(exc), 403)
        except OperationRefused as exc:
            return failure(str(exc), exc.status)
        except AdoptionRequired as exc:
            return failure(str(exc), 409, exc.as_data())
        except NotImplementedError:
            return failure("service not implemented in this phase", 501)
        except ValueError as exc:
            # The service raises ValueError with a message written for the administrator.
            return failure(str(exc) or "invalid operation or request", 400)
        except KeyError, TypeError:
            return failure("invalid operation or request", 400)
        except Exception as exc:
            logger.error("admin %s failed: %s", operation, describe_error(exc))
            reason = warehouse_reason(exc)
            if reason:
                return failure(f"administration failed: {reason}", 503)
            return failure("administration failed; no credentials are included in diagnostics", 503)
        finally:
            if not recorded:
                auth.event(principal_id, operation, "", outcome)

    return [Route("/api/admin/{operation:path}", endpoint, methods=["GET", "POST"])]

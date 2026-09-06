"""Current weather + forecast for the Schedule page.

Already exists as a chat tool (HOME_ASSISTANT_TOOLS' get_weather, dispatched in
engine.py) — this is the same HomeAssistantClient.get_weather call reachable directly,
since a weather panel refreshing itself doesn't need to go through the LLM.
"""
from fastapi import APIRouter, HTTPException, Request

from ..auth import require_user

router = APIRouter(prefix="/api/weather", tags=["weather"])


@router.get("")
async def weather_now(request: Request, forecast_type: str = "daily"):
    require_user(request)
    home_assistant = request.app.state.home_assistant
    if home_assistant is None:
        raise HTTPException(503, "Home Assistant is not configured")
    result = home_assistant.mcp_client.get_weather(forecast_type=forecast_type)
    if "error" in result:
        raise HTTPException(502, result["error"])
    return result

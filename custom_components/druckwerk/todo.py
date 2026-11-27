"""Queue as a todo list per printer: delete cancels, move reorders."""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError

from .entity import AppEntity
from .platform_helper import add_entities


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    add_entities(entry, async_add_entities, "todo", AppTodo)


class AppTodo(AppEntity, TodoListEntity):
    # No CREATE: adding a job needs a sliced file.
    _attr_supported_features = (
        TodoListEntityFeature.DELETE_TODO_ITEM | TodoListEntityFeature.MOVE_TODO_ITEM
    )

    @property
    def todo_items(self) -> list[TodoItem]:
        return [
            TodoItem(uid=i["uid"], summary=i["summary"], description=i.get("description"), status=TodoItemStatus.NEEDS_ACTION)
            for i in self.app.get("items", [])
        ]

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            await self._post(f"/api/queue/{uid}/cancel", {})
        await self.hub.async_request_refresh()

    async def async_move_todo_item(self, uid: str, previous_uid: str | None) -> None:
        # position is 0-based among this printer's waiting jobs; scope names the printer.
        waiting = [i["uid"] for i in self.app.get("items", []) if i["uid"] != uid]
        if previous_uid is None:
            pos = 0
        else:
            pos = waiting.index(previous_uid) + 1 if previous_uid in waiting else len(waiting)
        await self._post(f"/api/queue/{uid}/reorder", {"position": pos, "scope": self.serial})
        await self.hub.async_request_refresh()

    async def _post(self, path: str, body: dict) -> None:
        # No act-as: the todo list is for HA users who can see it.
        try:
            await self.hub.request("POST", path, body)
        except HomeAssistantError as err:
            raise HomeAssistantError(f"Druckwerk: {err}") from err

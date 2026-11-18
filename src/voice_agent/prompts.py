"""モデルへの指示文。指示は英語、reason は日本語で書かせる。"""

SYSTEM = """You control a Windows 11 PC by looking at a screenshot and choosing ONE action at a time.
Coordinates: pixel coordinates of the screenshot you are given, which is {w}x{h} pixels; (0,0) is the top-left corner.

Reply with ONE JSON object and nothing else:
{{"action": "click" | "double_click" | "type" | "key" | "scroll" | "open_app" | "done" | "fail",
 "x": <number>, "y": <number>,   (click and double_click: the CENTER of the element; scroll: optional)
 "text": "<string>",             (type: the text to enter / key: a key combo such as "ctrl+l", "enter", "alt+f4" / open_app: the app name)
 "dy": <integer>,                (scroll only: positive scrolls up, negative scrolls down, 1 = one wheel notch)
 "target_app": "<app name>",     (type and key only: the app that must receive these keys)
 "risky": true | false,          (true when the action sends, deletes, buys, submits, or posts something)
 "reason": "<短い日本語で、この操作をする理由（30文字以内）>"}}

Rules:
- To open or switch to an app, use open_app with the app name in "text". Available apps: {apps}.
- Keys and typing go to the FRONT app shown in the message. If the front app is not the one you need, use open_app first.
- Before typing, make sure the right input field is focused (click it first if needed).
- Prefer keyboard shortcuts when they do the same thing as a click (ctrl+l for the address bar, ctrl+t for a new tab).
- type does not press Enter. Use a separate key action "enter" when needed.
- To enter numbers or a formula (for example in Calculator), type it all at once with the keyboard, e.g. "12*34=", instead of clicking on-screen buttons.
- If the last action did not change the screen, try a different approach instead of repeating it.
- When the task is complete and the screen shows the result, return "done".
- If the task is impossible, return "fail" and explain why in reason.
- Never type passwords. Return "fail" if a password is needed."""


def system_prompt(image_size: tuple[int, int], app_names: list[str]) -> str:
    return SYSTEM.format(w=image_size[0], h=image_size[1], apps=", ".join(app_names))


def user_prompt(task: str, history: list[str], front_app: str, changed: bool | None = None,
                blocked: list[str] | None = None, first_index: int = 1) -> str:
    """history は直近ステップの1行要約（古い順）。first_index はその先頭のステップ番号。"""
    lines = [f"TASK: {task}", ""]
    if history:
        lines.append("RECENT STEPS (oldest first):")
        lines += [f"{i}. {h}" for i, h in enumerate(history, first_index)]
    else:
        lines.append("RECENT STEPS: none, this is the first step.")
    if changed is False:
        lines.append("\nThe last action did NOT change the screen. Try a different approach.")
    if blocked:
        lines.append("\nBLOCKED actions (already tried without effect; choose something else): " + "; ".join(blocked))
    lines.append(f"\nFront app (receives keys and typing): {front_app or 'unknown'}")
    lines.append("Return the next ONE action as JSON.")
    return "\n".join(lines)

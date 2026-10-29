---
title: Phone sidebar: a scroll that starts on a chat row's ⋮ button opens the row menu
labels: bug
---

## What

On a phone, scrolling the chat list keeps opening a row's ⋮ options menu. A scroll that starts with the finger on the ⋮ button, or on its enlarged 44px `mc-touch-hit` area along each row's right edge, opens the menu instead of scrolling.

## Cause

`website/src/pages/chat-sidebar/SessionRow.tsx` (the `isMobile` branch of `rowActions`) wraps the ⋮ button in Radix `DropdownMenuTrigger`. Radix's trigger opens on **pointerdown** for every pointer type (`@radix-ui/react-dropdown-menu` 2.1.18):

```js
onPointerDown: (event) => {
  if (!disabled && event.button === 0 && event.ctrlKey === false) {
    context.onOpenToggle();
    if (!context.open) event.preventDefault();
  }
}
```

So the menu opens the moment a finger lands, before the browser can tell a tap from a scroll, and `preventDefault()` also cancels the scroll. Upstream main (checked 2026-10-27, `e062153c45`) still does this.

## Fix we are running (for reference)

Commit: https://github.com/billygerhard/KiroCrew/commit/247d17ff4b

In the shared `components/ui/dropdown-menu.tsx` wrapper, `DropdownMenuTrigger` calls `preventDefault()` on a touch `pointerdown`, so Radix skips its handler. That doesn't cancel the scroll; only a `touchstart` preventDefault would. It then toggles the menu from the tap's `click`, which a scroll never produces. `DropdownMenu` provides its open state and setter through a small context for this. Mouse and pen still open on press, the same rule the composer chips follow (`test/pressActivationDropdowns.test.tsx`). Test: `website/src/test/DropdownMenu.touchTrigger.test.tsx`.

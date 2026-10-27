// A touch on a dropdown trigger opens the menu on the tap's `click`, never on
// `pointerdown`: on a phone a scroll whose finger landed on a list row's ⋮
// opened that row's menu, because Radix toggles on `pointerdown` for every
// pointer type. A scroll ends without a `click`, so it no longer opens a menu.
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

function Harness() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger data-testid="trigger">more</DropdownMenuTrigger>
      <DropdownMenuContent data-testid="content">
        <DropdownMenuItem>Rename</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

describe('DropdownMenuTrigger on touch', () => {
  it('does not open on a touch pointerdown alone (a scroll that starts on the trigger)', () => {
    render(<Harness />)
    fireEvent.pointerDown(screen.getByTestId('trigger'), { pointerType: 'touch', button: 0 })
    expect(screen.queryByTestId('content')).not.toBeInTheDocument()
  })

  it('opens on the tap that follows the touch, and a second tap closes it', () => {
    render(<Harness />)
    const trigger = screen.getByTestId('trigger')
    fireEvent.pointerDown(trigger, { pointerType: 'touch', button: 0 })
    fireEvent.click(trigger)
    expect(screen.getByTestId('content')).toBeInTheDocument()

    fireEvent.pointerDown(trigger, { pointerType: 'touch', button: 0 })
    fireEvent.click(trigger)
    expect(screen.queryByTestId('content')).not.toBeInTheDocument()
  })

  it('still opens a mouse press on pointerdown', () => {
    render(<Harness />)
    fireEvent.pointerDown(screen.getByTestId('trigger'), { pointerType: 'mouse', button: 0 })
    expect(screen.getByTestId('content')).toBeInTheDocument()
  })

  it('ignores a click with no touch press before it (keyboard and mouse go through Radix)', () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('trigger'))
    expect(screen.queryByTestId('content')).not.toBeInTheDocument()
  })
})

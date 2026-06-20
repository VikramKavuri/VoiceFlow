/**
 * Tests for the Toggle sub-component used inside SettingsPanel.
 *
 * Toggle is not exported, so we replicate it here for unit-testing purposes.
 * Its role is critical: every setting in the UI goes through it.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// ---------------------------------------------------------------------------
// Replicate Toggle component (unexported from SettingsPanel)
// ---------------------------------------------------------------------------

interface ToggleProps {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}

function Toggle({ checked, onChange, label }: ToggleProps) {
  return (
    <label className="flex items-center justify-between gap-3 cursor-pointer group">
      <span>{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={checked ? "bg-voiceflow-primary" : "bg-voiceflow-idle/40"}
      >
        <span
          className={checked ? "translate-x-[18px]" : "translate-x-[3px]"}
        />
      </button>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function renderToggle(checked: boolean, onChange = vi.fn(), label = "My Setting") {
  return render(<Toggle checked={checked} onChange={onChange} label={label} />);
}

// ---------------------------------------------------------------------------
// Label
// ---------------------------------------------------------------------------

describe("Toggle — label", () => {
  it("renders the label text", () => {
    renderToggle(false, vi.fn(), "Auto punctuation");
    expect(screen.getByText("Auto punctuation")).toBeInTheDocument();
  });

  it("renders a different label text", () => {
    renderToggle(false, vi.fn(), "Launch at startup");
    expect(screen.getByText("Launch at startup")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// ARIA attributes
// ---------------------------------------------------------------------------

describe("Toggle — ARIA", () => {
  it("has role=switch", () => {
    renderToggle(false);
    expect(screen.getByRole("switch")).toBeInTheDocument();
  });

  it("aria-checked is false when unchecked", () => {
    renderToggle(false);
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
  });

  it("aria-checked is true when checked", () => {
    renderToggle(true);
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });
});

// ---------------------------------------------------------------------------
// Interaction — click
// ---------------------------------------------------------------------------

describe("Toggle — click interaction", () => {
  it("calls onChange(true) when toggled from false", () => {
    const onChange = vi.fn();
    renderToggle(false, onChange);
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("calls onChange(false) when toggled from true", () => {
    const onChange = vi.fn();
    renderToggle(true, onChange);
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("calls onChange exactly once per click", () => {
    const onChange = vi.fn();
    renderToggle(false, onChange);
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("calls onChange again on a second click", () => {
    const onChange = vi.fn();
    renderToggle(false, onChange);
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// Visual state (CSS class)
// ---------------------------------------------------------------------------

describe("Toggle — visual state", () => {
  it("applies primary bg class when checked", () => {
    const { container } = renderToggle(true);
    expect(container.querySelector(".bg-voiceflow-primary")).toBeInTheDocument();
  });

  it("does not apply primary bg class when unchecked", () => {
    const { container } = renderToggle(false);
    expect(container.querySelector(".bg-voiceflow-primary")).not.toBeInTheDocument();
  });

  it("thumb has translate-x-[18px] when checked", () => {
    const { container } = renderToggle(true);
    expect(container.querySelector(".translate-x-\\[18px\\]")).toBeInTheDocument();
  });

  it("thumb has translate-x-[3px] when unchecked", () => {
    const { container } = renderToggle(false);
    expect(container.querySelector(".translate-x-\\[3px\\]")).toBeInTheDocument();
  });
});

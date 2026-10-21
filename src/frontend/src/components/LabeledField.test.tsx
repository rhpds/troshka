import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import LabeledField from "./LabeledField";

describe("LabeledField", () => {
  it("provides a distinct accessible label for repeated fields without adding a wrapper", () => {
    const { container } = render(<>
      <LabeledField label="Name"><input defaultValue="first" /></LabeledField>
      <LabeledField label="Name"><input defaultValue="second" /></LabeledField>
    </>);
    const inputs = screen.getAllByRole("textbox", { name: "Name" });
    expect(inputs[0].id).not.toBe(inputs[1].id);
    expect(Array.from(container.children).map((child) => child.tagName)).toEqual(["LABEL", "INPUT", "LABEL", "INPUT"]);
    expect(screen.getAllByLabelText("Name")).toEqual(inputs);
  });
  it("preserves explicit IDs, presentation, and change handlers", () => {
    const onChange = vi.fn();
    render(<LabeledField label="Password" className="field-label" style={{ display: "block" }}>
      <input id="existing-password" type="password" onChange={onChange} />
    </LabeledField>);
    const input = screen.getByLabelText("Password");
    expect(input.id).toBe("existing-password");
    expect(screen.getByText("Password")).toHaveClass("field-label");
    fireEvent.change(input, { target: { value: "secret" } });
    expect(onChange).toHaveBeenCalledOnce();
  });
  it("labels selects and textareas with dynamic label content", () => {
    render(<>
      <LabeledField label={<>Region <span>(required)</span></>}><select defaultValue="west"><option value="west">West</option></select></LabeledField>
      <LabeledField label="Notes"><textarea defaultValue="note" /></LabeledField>
    </>);
    expect(screen.getByRole("combobox", { name: "Region (required)" })).toHaveValue("west");
    expect(screen.getByRole("textbox", { name: "Notes" })).toHaveValue("note");
  });

  it("keeps input adornments in their existing layout container", () => {
    render(<LabeledField label="Token" controlWrapper={<div data-testid="control-row" />}
      afterControl={<button type="button">Reveal</button>}>
      <input type="password" />
    </LabeledField>);
    const row = screen.getByTestId("control-row");
    expect(screen.getByLabelText("Token").parentElement).toBe(row);
    expect(screen.getByRole("button", { name: "Reveal" }).parentElement).toBe(row);
  });
});

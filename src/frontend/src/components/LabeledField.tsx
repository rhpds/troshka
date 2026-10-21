"use client";

import { cloneElement, useId } from "react";
import type { LabelHTMLAttributes, ReactElement, ReactNode } from "react";

type LabeledFieldProps = Omit<LabelHTMLAttributes<HTMLLabelElement>, "children" | "htmlFor"> & {
  label: ReactNode;
  children: ReactElement<{ id?: string }>;
  controlWrapper?: ReactElement<{ children?: ReactNode }>;
  afterControl?: ReactNode;
};

/** Associate a visible label with its native input without adding a layout wrapper. */
export default function LabeledField({ label, children, controlWrapper, afterControl, ...labelProps }: Readonly<LabeledFieldProps>) {
  const generatedId = useId();
  const id = children.props.id || generatedId;
  const control = <>{cloneElement(children, { id })}{afterControl}</>;
  return (
    <>
      <label {...labelProps} htmlFor={id}>{label}</label>
      {controlWrapper ? cloneElement(controlWrapper, {}, control) : control}
    </>
  );
}

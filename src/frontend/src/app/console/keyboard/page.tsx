"use client";

import React, { Suspense, useState, useCallback, useEffect, useRef } from "react";
import { useSearchParams } from "next/navigation";
import SKKeyboard from "react-simple-keyboard";
import "react-simple-keyboard/build/css/index.css";

const XK = {
  Ctrl: 0xffe3, Alt: 0xffe9, Shift: 0xffe1, Super: 0xffeb,
  Tab: 0xff09, Esc: 0xff1b, Del: 0xffff, Return: 0xff0d,
  F1: 0xffbe, F2: 0xffbf, F3: 0xffc0, F4: 0xffc1, F5: 0xffc2, F6: 0xffc3,
  F7: 0xffc4, F8: 0xffc5, F9: 0xffc6, F10: 0xffc7, F11: 0xffc8, F12: 0xffc9,
} as const;

const funcKeysyms: Record<string, number> = {
  "{bksp}": 0xff08, "{enter}": 0xff0d, "{tab}": 0xff09, "{space}": 0x0020,
  "{esc}": 0xff1b, "{del}": XK.Del,
  "{f1}": XK.F1, "{f2}": XK.F2, "{f3}": XK.F3, "{f4}": XK.F4,
  "{f5}": XK.F5, "{f6}": XK.F6, "{f7}": XK.F7, "{f8}": XK.F8,
  "{f9}": XK.F9, "{f10}": XK.F10, "{f11}": XK.F11, "{f12}": XK.F12,
};

const shiftedSymbols = '~!@#$%^&*()_+{}|:"<>?';

const kbLayouts = {
  default: [
    "{esc} {f1} {f2} {f3} {f4} {f5} {f6} {f7} {f8} {f9} {f10} {f11} {f12} {del}",
    "` 1 2 3 4 5 6 7 8 9 0 - = {bksp}",
    "{tab} q w e r t y u i o p [ ] \\",
    "{lock} a s d f g h j k l ; ' {enter}",
    "{shift} z x c v b n m , . / {shift}",
    "{ctrl} {alt} {space} {alt} {ctrl}",
  ],
  shift: [
    "{esc} {f1} {f2} {f3} {f4} {f5} {f6} {f7} {f8} {f9} {f10} {f11} {f12} {del}",
    "~ ! @ # $ % ^ & * ( ) _ + {bksp}",
    "{tab} Q W E R T Y U I O P { } |",
    '{lock} A S D F G H J K L : " {enter}',
    "{shift} Z X C V B N M < > ? {shift}",
    "{ctrl} {alt} {space} {alt} {ctrl}",
  ],
};

const kbDisplay: Record<string, string> = {
  "{esc}": "Esc", "{del}": "Del", "{bksp}": "⌫", "{tab}": "Tab ⇥",
  "{lock}": "Caps", "{enter}": "Enter ↵", "{shift}": "⇧ Shift",
  "{ctrl}": "Ctrl", "{alt}": "Alt", "{space}": " ",
  "{f1}": "F1", "{f2}": "F2", "{f3}": "F3", "{f4}": "F4",
  "{f5}": "F5", "{f6}": "F6", "{f7}": "F7", "{f8}": "F8",
  "{f9}": "F9", "{f10}": "F10", "{f11}": "F11", "{f12}": "F12",
};

export default function KeyboardPageWrapper() {
  return (
    <Suspense fallback={<div style={{ background: "#0d0d1a", height: "100vh" }} />}>
      <KeyboardPage />
    </Suspense>
  );
}

function KeyboardPage() {
  const searchParams = useSearchParams();
  const vmName = searchParams.get("name") || "VM";
  const [kbLayout, setKbLayout] = useState("default");
  const [ctrlSticky, setCtrlSticky] = useState(false);
  const [altSticky, setAltSticky] = useState(false);
  const [connected, setConnected] = useState(true);
  const kbRef = useRef<any>(null);

  useEffect(() => {
    document.title = `Keyboard: ${vmName}`;
    if (!window.opener) setConnected(false);
  }, [vmName]);

  useEffect(() => {
    if (!window.opener) return;
    const check = setInterval(() => {
      if (!window.opener || (window.opener as any).closed) {
        setConnected(false);
        clearInterval(check);
      }
    }, 1000);
    return () => clearInterval(check);
  }, []);

  const handlePress = useCallback((button: string) => {
    if (button === "{shift}" || button === "{lock}") {
      setKbLayout((prev) => (prev === "default" ? "shift" : "default"));
      return;
    }
    if (button === "{ctrl}") { setCtrlSticky((p) => !p); return; }
    if (button === "{alt}") { setAltSticky((p) => !p); return; }

    let keysym: number;
    if (button in funcKeysyms) {
      keysym = funcKeysyms[button];
    } else if (button.length === 1) {
      keysym = button.charCodeAt(0);
    } else {
      return;
    }

    const combo: number[] = [];
    if (ctrlSticky) combo.push(XK.Ctrl);
    if (altSticky) combo.push(XK.Alt);
    if (button.length === 1 && ((button >= "A" && button <= "Z") || shiftedSymbols.includes(button))) {
      combo.push(XK.Shift);
    }
    combo.push(keysym);

    if (window.opener) {
      window.opener.postMessage({ type: "vkb-combo", keysyms: combo }, window.location.origin);
    }

    if (ctrlSticky) setCtrlSticky(false);
    if (altSticky) setAltSticky(false);
    if (kbLayout === "shift" && button !== "{shift}" && button !== "{lock}") {
      setKbLayout("default");
    }
  }, [kbLayout, ctrlSticky, altSticky]);

  const buttonTheme = [
    ...(ctrlSticky ? [{ class: "vkb-sticky", buttons: "{ctrl}" }] : []),
    ...(altSticky ? [{ class: "vkb-sticky", buttons: "{alt}" }] : []),
  ];

  if (!connected) {
    return (
      <div style={{ background: "#0d0d1a", color: "#888", height: "100vh", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14 }}>
        Console window closed. You can close this window.
      </div>
    );
  }

  return (
    <div
      onMouseDown={(e) => e.preventDefault()}
      style={{ background: "#0d0d1a", height: "100vh", padding: "8px", boxSizing: "border-box", overflow: "hidden" }}
    >
      <div style={{ fontSize: 11, color: "#555", padding: "0 4px 4px", userSelect: "none" }}>{vmName}</div>
      <SKKeyboard
        keyboardRef={(r: any) => (kbRef.current = r)}
        layout={kbLayouts}
        layoutName={kbLayout}
        display={kbDisplay}
        onKeyPress={handlePress}
        theme="hg-theme-dark"
        physicalKeyboardHighlight={false}
        preventMouseDownDefault={true}
        disableCaretPositioning={true}
        buttonTheme={buttonTheme.length ? buttonTheme : undefined}
        useMouseEvents={true}
      />
      <style>{`
        html, body { margin: 0; padding: 0; overflow: hidden; background: #0d0d1a; }
        .hg-theme-dark { background: transparent; font-family: system-ui, sans-serif; }
        .hg-theme-dark .hg-row { display: flex; gap: 3px; margin-bottom: 3px; }
        .hg-theme-dark .hg-button {
          background: #1a1a2e; color: #bbb; border: 1px solid #333; border-radius: 4px;
          padding: 8px 2px; min-width: 30px; min-height: 28px; font-size: 11px;
          cursor: pointer; display: flex; align-items: center; justify-content: center;
          box-shadow: 0 2px 0 #0a0a15; flex-grow: 1; user-select: none;
        }
        .hg-theme-dark .hg-button:hover { background: #252545; color: #fff; }
        .hg-theme-dark .hg-button:active, .hg-theme-dark .hg-button.hg-activeButton {
          background: #2a2a5a; box-shadow: none; transform: translateY(1px);
        }
        .hg-theme-dark .hg-button[data-skbtn="{space}"] { flex-grow: 10; }
        .hg-theme-dark .hg-button[data-skbtn="{bksp}"],
        .hg-theme-dark .hg-button[data-skbtn="{tab}"] { flex-grow: 2; }
        .hg-theme-dark .hg-button[data-skbtn="{enter}"],
        .hg-theme-dark .hg-button[data-skbtn="{lock}"] { flex-grow: 2.5; }
        .hg-theme-dark .hg-button[data-skbtn="{shift}"] { flex-grow: 3; }
        .hg-theme-dark .hg-button[data-skbtn="{ctrl}"],
        .hg-theme-dark .hg-button[data-skbtn="{alt}"] { flex-grow: 2; min-width: 50px; }
        .hg-theme-dark .hg-button[data-skbtn="{shift}"],
        .hg-theme-dark .hg-button[data-skbtn="{lock}"],
        .hg-theme-dark .hg-button[data-skbtn="{ctrl}"],
        .hg-theme-dark .hg-button[data-skbtn="{alt}"],
        .hg-theme-dark .hg-button[data-skbtn="{tab}"],
        .hg-theme-dark .hg-button[data-skbtn="{enter}"],
        .hg-theme-dark .hg-button[data-skbtn="{bksp}"] { background: #131328; }
        .hg-theme-dark .hg-button[data-skbtn="{esc}"] { background: #2a1515; color: #f87171; }
        .hg-theme-dark .hg-button.vkb-sticky {
          background: rgba(74,222,128,0.2) !important; border-color: #4ade80; color: #4ade80;
        }
      `}</style>
    </div>
  );
}

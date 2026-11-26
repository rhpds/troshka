import { describe, expect, it } from "vitest";
import { ReadableStream } from "node:stream/web";
import { readEvents } from "./assistant-client";

describe("assistant SSE", () => {
  it("preserves split UTF-8 and event boundaries", async () => {
    const bytes = new TextEncoder().encode('data: {"type":"answer_delta","payload":{"text":"café"}}\n\n');
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      for (let i = 0; i < bytes.length; i++) controller.enqueue(bytes.slice(i, i + 1));
      controller.close();
    } });
    const texts: unknown[] = [];
    await readEvents(stream as unknown as globalThis.ReadableStream<Uint8Array>, event => texts.push(event.payload.text));
    expect(texts).toEqual(["café"]);
  });

  it("rejects incomplete events", async () => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":'));
      controller.close();
    } });
    await expect(readEvents(stream as unknown as globalThis.ReadableStream<Uint8Array>, () => {})).rejects.toThrow("unexpectedly");
  });
});

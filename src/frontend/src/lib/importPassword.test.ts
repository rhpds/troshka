import { afterEach, describe, expect, it, vi } from "vitest";
import { generateImportPassword } from "./importPassword";

afterEach(() => vi.restoreAllMocks());

describe("generateImportPassword", () => {
  it("uses cryptographic randomness and preserves the import password format", () => {
    const weakRandom = vi.spyOn(Math, "random").mockImplementation(() => {
      throw new Error("Passwords must not use Math.random");
    });
    const password = generateImportPassword();
    expect(password).toMatch(/^[a-km-np-zA-HJ-NP-Z2-9]{12}$/);
    expect(weakRandom).not.toHaveBeenCalled();
  });

  it("rejects out-of-range bytes instead of biasing character selection", () => {
    const random = vi.spyOn(crypto, "getRandomValues")
      .mockImplementationOnce((array) => {
        (array as Uint8Array).fill(255);
        return array;
      })
      .mockImplementation((array) => {
        (array as Uint8Array).fill(0);
        return array;
      });
    expect(generateImportPassword()).toBe("aaaaaaaaaaaa");
    expect(random).toHaveBeenCalledTimes(2);
  });
});

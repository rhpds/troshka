declare module "@novnc/novnc" {
  export default class RFB {
    constructor(target: HTMLElement, urlOrChannel: string | WebSocket, options?: Record<string, unknown>);
    disconnect(): void;
    sendCredentials(credentials: { password: string }): void;
    scaleViewport: boolean;
    resizeSession: boolean;
    clipViewport: boolean;
    qualityLevel: number;
    compressionLevel: number;
    addEventListener(type: string, listener: (e: CustomEvent) => void): void;
  }
}

export const WETTY_IMAGE = "quay.io/rhpds/wetty:v2.5";

export interface WettyAttrs {
  basePath: string;
  port: number;
  sshHost: string;
  sshPort: number;
  sshUser: string;
  sshPass: string;
}

export function isWettyContainer(container: { name?: string; image?: string }): boolean {
  const name = container.name || "";
  const image = container.image || "";
  return name.startsWith("wetty-") || image.includes("wetty");
}

export function parseWettyCommand(
  command: string | string[] | null | undefined,
): WettyAttrs {
  const attrs: WettyAttrs = {
    basePath: "",
    port: 8001,
    sshHost: "",
    sshPort: 22,
    sshUser: "",
    sshPass: "",
  };
  const args = Array.isArray(command) ? command : command ? [command] : [];
  for (const arg of args) {
    if (arg.startsWith("--base=")) {
      attrs.basePath = arg.slice(7).replace(/^\/+|\/+$/g, "");
    } else if (arg.startsWith("--port=")) {
      attrs.port = parseInt(arg.slice(6), 10) || attrs.port;
    } else if (arg.startsWith("--ssh-host=")) {
      attrs.sshHost = arg.slice(11);
    } else if (arg.startsWith("--ssh-port=")) {
      attrs.sshPort = parseInt(arg.slice(11), 10) || 22;
    } else if (arg.startsWith("--ssh-user=")) {
      attrs.sshUser = arg.slice(11);
    } else if (arg.startsWith("--ssh-pass=")) {
      attrs.sshPass = arg.slice(11);
    }
  }
  return attrs;
}

export function buildWettyCommand(attrs: WettyAttrs): string[] {
  const base = (attrs.basePath || "wetty").replace(/^\/+|\/+$/g, "");
  return [
    `--base=/${base}/`,
    `--port=${attrs.port}`,
    `--ssh-host=${attrs.sshHost}`,
    `--ssh-port=${attrs.sshPort}`,
    `--ssh-user=${attrs.sshUser}`,
    "--ssh-auth=password",
    `--ssh-pass=${attrs.sshPass}`,
  ];
}

export function formatCommandForInput(command: string | string[] | null | undefined): string {
  if (!command) return "";
  if (Array.isArray(command)) return command.join(" ");
  return command;
}

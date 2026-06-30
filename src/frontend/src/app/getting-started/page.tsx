"use client";

import React from "react";
import {
  PageSection,
  Title,
  Content,
  ContentVariants,
} from "@patternfly/react-core";

interface StepProps {
  number: number;
  title: string;
  image?: string;
  imageAlt?: string;
  children: React.ReactNode;
}

function Step({ number, title, image, imageAlt, children }: StepProps) {
  return (
    <div style={{
      display: "flex",
      gap: 24,
      marginBottom: 40,
      alignItems: "flex-start",
      paddingLeft: number ? 0 : 60,
    }}>
      {number > 0 && (
        <div style={{
          minWidth: 36,
          height: 36,
          borderRadius: "50%",
          background: "rgba(108,99,255,0.2)",
          color: "#a78bfa",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontWeight: 700,
          fontSize: 16,
          flexShrink: 0,
        }}>
          {number}
        </div>
      )}
      <div style={{ flex: 1 }}>
        {title && <h3 style={{ margin: "0 0 8px 0", fontSize: 18, fontWeight: 600 }}>{title}</h3>}
        <div style={{ fontSize: 14, lineHeight: 1.7, opacity: 0.85, marginBottom: image ? 16 : 0 }}>
          {children}
        </div>
        {image && (
          <img
            src={image}
            alt={imageAlt || title}
            style={{
              maxWidth: "100%",
              borderRadius: 8,
              border: "1px solid var(--pf-t--global--border--color--default)",
              boxShadow: "0 2px 8px rgba(0,0,0,0.3)",
            }}
          />
        )}
      </div>
    </div>
  );
}

function Tip({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      background: "rgba(108,99,255,0.08)",
      border: "1px solid rgba(108,99,255,0.2)",
      borderRadius: 8,
      padding: "10px 14px",
      fontSize: 13,
      marginTop: 8,
      lineHeight: 1.6,
    }}>
      <strong>Tip:</strong> {children}
    </div>
  );
}

const tocItems = [
  { label: "Try It: Import an Example Template", href: "#try-it" },
  { label: "Build Your First Environment", href: "#build" },
  { label: "Open the VM Console", href: "#console" },
  { label: "Port Forwarding", href: "#port-forwarding" },
  { label: "Quick Start Templates", href: "#quick-starts" },
  { label: "Import Template YAML", href: "#import-yaml" },
];

function SectionHeading({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <h2 id={id} style={{ fontSize: 22, fontWeight: 600, marginTop: 48, marginBottom: 32, paddingBottom: 12, borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
      {children}
    </h2>
  );
}

export default function GettingStartedPage() {
  return (
    <>
      <PageSection>
        <Title headingLevel="h1" size="2xl">Getting Started</Title>
        <Content component={ContentVariants.p} style={{ marginTop: 8, maxWidth: 700, opacity: 0.7 }}>
          Troshka lets you design and deploy nested virtual environments with a visual drag-and-drop editor.
          This guide walks you through building your first VM with networking, storage, and internet access.
        </Content>
      </PageSection>

      <PageSection style={{ maxWidth: 900 }}>
        <nav style={{
          background: "rgba(255,255,255,0.03)",
          border: "1px solid var(--pf-t--global--border--color--default)",
          borderRadius: 8,
          padding: "16px 20px",
          marginBottom: 40,
        }}>
          <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 10, opacity: 0.6 }}>Contents</div>
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
            {tocItems.map((item) => (
              <li key={item.href}>
                <a
                  href={item.href}
                  style={{ color: "#a78bfa", textDecoration: "none", fontSize: 14 }}
                  onMouseOver={(e) => { (e.target as HTMLElement).style.textDecoration = "underline"; }}
                  onMouseOut={(e) => { (e.target as HTMLElement).style.textDecoration = "none"; }}
                >
                  {item.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <SectionHeading id="try-it">Try It: Import an Example Template</SectionHeading>

        <Step number={0} title="">
          <p>
            The fastest way to get started is to import an example template that creates a ready-to-go
            web server VM with networking and port forwarding already configured.
          </p>
        </Step>

        <Step number={1} title="Download the example template">
          <p>
            Download <a href="https://github.com/rhpds/troshka/blob/main/example_templates/test-web.yaml" target="_blank" rel="noopener" style={{ color: "#a78bfa" }}>test-web.yaml</a> from
            the Troshka repository. Click the <strong>Raw</strong> button on GitHub, then save the file (Ctrl+S / Cmd+S),
            or use the command line:
          </p>
          <pre style={{
            background: "rgba(255,255,255,0.05)",
            border: "1px solid var(--pf-t--global--border--color--default)",
            borderRadius: 6,
            padding: "10px 14px",
            fontSize: 13,
            marginTop: 10,
            overflowX: "auto",
          }}>
{`curl -LO https://raw.githubusercontent.com/rhpds/troshka/main/example_templates/test-web.yaml`}
          </pre>
          <Tip>
            This template creates a RHEL 10 VM running Apache httpd, connected to a 10.0.0.0/24 network
            with a gateway that forwards port 80 to the VM. Cloud-init installs and starts httpd automatically.
          </Tip>
        </Step>

        <Step number={2} title="Create a blank project">
          <p>
            Go to the <strong>Projects</strong> page and click <strong>New Project</strong>.
            Select <strong>Blank Project</strong>, give it a name, and click <strong>Create</strong>.
            You{"'"}ll land on an empty canvas with an <strong>Import Template YAML</strong> overlay.
          </p>
        </Step>

        <Step number={3} title="Import the template">
          <p>
            Click <strong>Import Template YAML</strong>. In the modal, paste the contents of <code>test-web.yaml</code> or
            click <strong>Upload</strong> and select the file. Click <strong>Import</strong>. The canvas will populate
            with a VM, network, gateway, disk, and ISO — all wired together and ready to deploy.
          </p>
        </Step>

        <Step number={4} title="Set the password and deploy">
          <p>
            Click the <strong>VM</strong> node on the canvas. In the properties panel, scroll to <strong>Cloud Init</strong> and
            set a <strong>password</strong> for the cloud user. Then click <strong>Deploy</strong> in the top toolbar.
            Once the project is active, the VM will be running httpd — open the console or hit port 80
            on the external IP to see it.
          </p>
        </Step>

        <SectionHeading id="build">Build Your First Environment</SectionHeading>

        <Step number={1} title="Create a new project" image="/images/guide/01-new-project-modal.png">
          <p>
            Click the <strong>New Project</strong> button on the Projects page. In the modal, select <strong>Blank Project</strong> to start
            with an empty canvas. Give your project a name and click <strong>Create</strong>.
          </p>
        </Step>

        <Step number={2} title="Add a network" image="/images/guide/02-drag-network.png">
          <p>
            In the left palette, expand the <strong>Networking</strong> section. Drag a <strong>Network</strong> onto the canvas.
            This creates a virtual bridge that your VMs will connect to. You can configure the CIDR and DHCP settings in
            the properties panel on the right.
          </p>
          <Tip>The default CIDR is 192.168.1.0/24 with DHCP enabled — good for most setups.</Tip>
        </Step>

        <Step number={3} title="Add a VM" image="/images/guide/03-drag-vm.png">
          <p>
            From the <strong>Compute</strong> section, drag a <strong>VM</strong> onto the canvas.
            Each VM starts with 2 vCPUs, 4 GB RAM, and one network interface (NIC).
            Click the VM to configure its name, CPU, and memory in the properties panel.
          </p>
        </Step>

        <Step number={4} title="Connect the VM to the network" image="/images/guide/04-connect-vm-network.png">
          <p>
            Drag from the small circular handle on the VM node to the network node to connect them.
            This attaches the VM{"'"}s NIC to the network. The VM will get an IP address via DHCP or you can
            set a static IP in the properties panel.
          </p>
        </Step>

        <Step number={5} title="Add storage" image="/images/guide/05-drag-storage.png">
          <p>
            From the <strong>Storage</strong> section, drag a <strong>Disk</strong> onto the canvas and connect it to your VM.
            Set the disk size in the properties panel. Then drag an <strong>ISO</strong> and connect it to the VM —
            this will be the installation media (e.g., RHEL, Ubuntu).
          </p>
          <Tip>Select a library image for the disk or ISO in the properties panel to use a pre-uploaded image.</Tip>
        </Step>

        <Step number={6} title="Add a gateway for internet access" image="/images/guide/06-drag-gateway.png">
          <p>
            Drag a <strong>Gateway</strong> from the Networking section and <strong>connect it to your network node</strong>.
            The gateway must be linked to the network — drag from the gateway{"'"}s handle to the network{"'"}s handle to
            create the connection. Once connected, the gateway provides outbound internet access via NAT so your VMs
            can reach external repositories and services.
          </p>
          <Tip>Without a gateway, VMs on the network have no route to the internet. The gateway creates a NAT namespace that bridges internal traffic to the host{"'"}s network.</Tip>
        </Step>

        <Step number={7} title="Deploy" image="/images/guide/07-deploy-button.png">
          <p>
            Click the <strong>Deploy</strong> button in the top toolbar. Troshka will provision the network,
            create the VM disks, and start your environment. You can watch the progress in real time.
            Once deployed, the project state changes to <strong style={{ color: "#4ade80" }}>active</strong>.
          </p>
        </Step>

        <SectionHeading id="console">Open the VM Console</SectionHeading>

        <Step number={0} title="" image="/images/guide/11-vm-console.png">
          <p>
            After deployment, each running VM shows action buttons at the bottom of the node on the canvas.
            Click the <strong>Console</strong> button (monitor icon) to open a VNC console in a new tab
            where you can interact with the VM directly — no SSH required.
          </p>
        </Step>

        <SectionHeading id="port-forwarding">Port Forwarding</SectionHeading>

        <Step number={1} title="Switch gateway mode" image="/images/guide/10-gateway-mode.png">
          <p>
            To expose VM services externally, click the <strong>gateway</strong> node on the canvas to open its properties.
            Change the <strong>Mode</strong> dropdown from {'"'}NAT (outbound only){'"'} to {'"'}NAT + Port Forwarding{'"'}.
          </p>
        </Step>

        <Step number={2} title="Add port forward rules" image="/images/guide/10-port-forwarding.png">
          <p>
            In the <strong>Port Forwarding</strong> section that appears, click <strong>+ Add Port Forward</strong>.
            For each rule, configure:
          </p>
          <ul style={{ margin: "8px 0", paddingLeft: 20 }}>
            <li><strong>External IP</strong> — select from allocated IPs (use the External IPs panel in the sidebar to add them)</li>
            <li><strong>Ext Port</strong> — the port exposed to the outside (e.g., 443)</li>
            <li><strong>Internal IP</strong> — pick a VM from the dropdown or enter a custom IP</li>
            <li><strong>Int Port</strong> — the port on the VM (e.g., 443)</li>
          </ul>
          <Tip>You can add multiple rules to forward different ports to different VMs on the same network.</Tip>
        </Step>

        <SectionHeading id="quick-starts">Quick Start Templates</SectionHeading>

        <Step number={0} title="" image="/images/guide/08-quick-starts.png">
          <p>
            Instead of building from scratch, you can use <strong>Quick Start</strong> templates. These are pre-configured
            topologies for common setups like OpenShift clusters. From the New Project modal,
            click <strong>Quick Starts</strong> to see the available templates.
          </p>
          <p style={{ marginTop: 8 }}>
            Each template shows an estimated deploy time. After selecting a template, you can customize settings
            like the cluster name, base domain, OCP version, and passwords before deploying.
          </p>
        </Step>

        <SectionHeading id="import-yaml">Import Template YAML</SectionHeading>

        <Step number={0} title="" image="/images/guide/09-import-yaml.png">
          <p>
            You can also import a pre-built topology from a YAML file. On a blank canvas, click the
            {" "}<strong>Import Template YAML</strong> button, then paste or upload your template.
            The YAML must contain <code>vms</code> and <code>networks</code> sections.
            After import, the canvas auto-layouts the topology and you can edit it before deploying.
          </p>
        </Step>
      </PageSection>
    </>
  );
}

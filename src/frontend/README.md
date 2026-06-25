# Troshka Frontend

Next.js 15 + PatternFly 6 frontend for the Troshka nested VM environment builder.

## Tech Stack

- Next.js 15 (App Router with Turbopack)
- TypeScript
- PatternFly 6 (React components and design system)
- Zustand (state management for canvas)
- React Flow (@xyflow/react) — topology canvas editor
- noVNC (@novnc/novnc) — VNC console client

## Development

```bash
# Install dependencies
npm install

# Run development server (hot-reloads)
npm run dev

# Build for production
npm run build
```

The development server runs at http://localhost:3100

## API Integration

The Next.js config proxies `/api/*` and `/ws/*` requests to the backend at http://localhost:8200.

## Project Structure

```
src/
├── app/
│   ├── layout.tsx              # Root layout with PatternFly Page, Masthead, Sidebar
│   ├── page.tsx                # Home page (redirects to /projects)
│   ├── login/                  # Authentication pages
│   ├── projects/               # Project list and detail (canvas editor)
│   ├── library/                # VM images, snapshots, ISOs
│   ├── settings/               # User settings (SSH keys, pull secret, registry, API keys)
│   ├── console/                # VNC console (noVNC) + virtual keyboard
│   ├── portal/                 # Portal access (token-based project viewer)
│   └── admin/                  # User, provider, host, storage pool management
├── components/
│   └── canvas/
│       ├── Canvas.tsx           # React Flow canvas wrapper
│       ├── Palette.tsx          # Left sidebar — add nodes, project settings, clock
│       ├── PropertiesPanel.tsx  # Right sidebar — node configuration
│       ├── StartOrderPanel.tsx  # VM boot order configuration
│       ├── LibraryPicker.tsx    # Disk image/ISO selection modal
│       ├── SavePatternModal.tsx # Pattern save with clock target option
│       ├── BulkDeployModal.tsx  # Deploy N copies of a pattern
│       ├── ExternalIpsPanel.tsx # EIP/port forwarding configuration
│       ├── CanvasToolbar.tsx    # Top toolbar (deploy, save, export)
│       └── nodes/
│           ├── VMNode.tsx       # Virtual machine node
│           ├── ContainerNode.tsx# Container and pod nodes
│           ├── NetworkNode.tsx  # Network bridge node
│           └── StorageNode.tsx  # Disk/ISO node
└── stores/
    └── canvasStore.ts           # Zustand store for topology state
```

## Key Patterns

- **`"use client"`** directive on all pages
- **Raw `fetch()`** for API calls (no React Query / TanStack Query)
- **`useState` + `useEffect`** for component-level state
- **Zustand** (`useCanvasStore`) for canvas topology state (nodes, edges, selections)
- **Auto-save**: topology changes are debounced 1s before saving to backend
- **PatternFly 6**: `PageSection`, `Toolbar`, `Card`, `Button`, `Modal`, `Switch`

## Authentication

User authentication state is stored in localStorage:
- `troshka-token`: JWT bearer token
- `troshka-user`: User object (id, email, role)

Dev mode auto-authenticates as admin (no login required).

## Canvas Editor

The drag-and-drop topology editor supports:

| Node | Description |
|------|-------------|
| VM | Virtual machine with NICs, disk controllers, cloud-init, boot devices |
| Container | Podman container with image, command, ports, env, volumes |
| Pod | Multi-container group sharing a network namespace (init + app containers) |
| Network | Virtual bridge with DHCP, DNS, PXE boot, security rules |
| Storage | Disk (qcow2/raw) or ISO image from library |

Containers and pods both use `ContainerNode.tsx` — pods are distinguished by `isPod: true` in node data.

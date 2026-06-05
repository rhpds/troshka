# Troshka Frontend

Next.js 15 + PatternFly 6 frontend for the Troshka nested VM environment builder.

## Tech Stack

- Next.js 15 (App Router with Turbopack)
- TypeScript
- PatternFly 6 (React components and design system)
- React Query (@tanstack/react-query)
- Zustand (state management)
- React Flow (@xyflow/react) - for canvas editor (Phase 4)

## Development

```bash
# Install dependencies
npm install

# Run development server
npm run dev

# Build for production
npm run build

# Start production server
npm start
```

The development server runs at http://localhost:3000

## API Integration

The Next.js config proxies `/api/*` and `/ws/*` requests to the backend at http://localhost:8000.

## Project Structure

```
src/
├── app/
│   ├── layout.tsx          # Root layout with PatternFly Page, Masthead, Sidebar
│   ├── page.tsx            # Home page (redirects to /projects)
│   ├── login/              # Authentication pages
│   ├── projects/           # Project list and detail (canvas editor coming in Phase 4)
│   ├── library/            # VM templates, snapshots, ISOs (Phase 7)
│   └── admin/              # User, provider, and host management (Phase 2)
public/
└── images/
    └── troshka-logo-*.png  # Logo files (16, 32, 48, 200px)
```

## Features

### Implemented (Phase 3)

- Dark/light theme toggle (persisted to localStorage)
- Responsive layout with collapsible sidebar
- Navigation: Projects, Library, Admin (Users, Providers, Hosts)
- Login/Register pages with backend API integration
- Projects list page (fetches from /api/v1/projects)
- Empty state components for placeholder pages
- Favicon and branding

### Coming Soon

- Phase 4: Canvas editor with React Flow for VM topology
- Phase 5: Real-time updates via WebSocket
- Phase 7: Library/catalog management

## PatternFly 6 Notes

This project uses PatternFly 6, which has some API differences from PF5:

- `EmptyStateHeader` and `EmptyStateIcon` must be imported from submodules:
  ```ts
  import { EmptyStateHeader } from "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateHeader";
  import { EmptyStateIcon } from "@patternfly/react-core/dist/esm/components/EmptyState/EmptyStateIcon";
  ```
- `Page` component with `isManagedSidebar` handles sidebar state automatically
- Dark theme is enabled by adding `pf-v6-theme-dark` class to `<html>`

## Authentication

User authentication state is stored in localStorage:
- `troshka-token`: JWT bearer token
- `troshka-user`: User object (id, email, role)

The Projects page checks for a valid token and redirects to `/login` if not authenticated.

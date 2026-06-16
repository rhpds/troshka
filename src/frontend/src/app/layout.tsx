"use client";

import "@patternfly/react-core/dist/styles/base.css";
import "./globals.css";
import React, { useState, useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  Button,
  Masthead,
  MastheadBrand,
  MastheadContent,
  MastheadMain,
  MastheadToggle,
  Nav,
  NavItem,
  NavList,
  Page,
  PageSidebar,
  PageSidebarBody,
  PageToggleButton,
  Toolbar,
  ToolbarContent,
  ToolbarGroup,
  ToolbarItem,
} from "@patternfly/react-core";
import BarsIcon from "@patternfly/react-icons/dist/esm/icons/bars-icon";
import SunIcon from "@patternfly/react-icons/dist/esm/icons/sun-icon";
import MoonIcon from "@patternfly/react-icons/dist/esm/icons/moon-icon";
import UserIcon from "@patternfly/react-icons/dist/esm/icons/user-icon";
import SignOutAltIcon from "@patternfly/react-icons/dist/esm/icons/sign-out-alt-icon";

interface UserInfo {
  id: string;
  email: string;
  role: string;
  display_name?: string;
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [isDark, setIsDark] = useState(true);
  const [user, setUser] = useState<UserInfo | null>(null);

  useEffect(() => {
    const saved = localStorage.getItem("troshka-theme");
    if (saved === "light") {
      setIsDark(false);
      document.documentElement.classList.remove("pf-v6-theme-dark");
    } else {
      document.documentElement.classList.add("pf-v6-theme-dark");
    }

    fetch("/api/v1/auth/me")
      .then((r) => {
        if (!r.ok) { setUser(null); return null; }
        return r.json();
      })
      .then((data) => { if (data) setUser(data); })
      .catch(() => setUser(null));
  }, []);

  useEffect(() => {
    const titleMap: Record<string, string> = {
      "/projects": "Projects",
      "/library/images": "Images",
      "/library/patterns": "Patterns",
      "/settings": "Settings",
      "/admin/users": "Users",
      "/admin/providers": "Providers",
      "/admin/hosts": "Hosts",
      "/admin/storage-pools": "Storage Pools",
      "/admin/dns-providers": "DNS Providers",
    };
    const pageTitle = titleMap[pathname || ""] || "";
    if (pageTitle) document.title = `Troshka: ${pageTitle}`;
  }, [pathname]);

  const toggleTheme = () => {
    setIsDark((prev) => {
      const next = !prev;
      if (next) {
        document.documentElement.classList.add("pf-v6-theme-dark");
        localStorage.setItem("troshka-theme", "dark");
      } else {
        document.documentElement.classList.remove("pf-v6-theme-dark");
        localStorage.setItem("troshka-theme", "light");
      }
      return next;
    });
  };

  const handleLogout = () => {
    localStorage.removeItem("troshka-token");
    localStorage.removeItem("troshka-user");
    setUser(null);
    router.push("/login");
  };

  const isLoginPage = pathname === "/login";
  const isConsolePage = pathname?.startsWith("/console");
  const isPortalPage = pathname?.startsWith("/portal");
  const isAuthenticated = !!user && !isLoginPage;
  const isAdmin = user?.role === "admin";

  const [backendDown, setBackendDown] = useState(false);
  useEffect(() => {
    let failures = 0;
    const check = () => {
      fetch("/api/v1/auth/me", { signal: AbortSignal.timeout(5000) })
        .then((r) => { failures = r.ok || r.status === 401 ? 0 : failures + 1; setBackendDown(failures >= 2); })
        .catch(() => { failures++; setBackendDown(failures >= 2); });
    };
    check();
    const iv = setInterval(check, 5000);
    return () => clearInterval(iv);
  }, []);

  const [navWarnings, setNavWarnings] = useState<{ hosts: boolean; pools: boolean }>({ hosts: false, pools: false });
  useEffect(() => {
    if (!isAdmin) return;
    const check = () => {
      fetch("/api/v1/hosts/").then(r => r.ok ? r.json() : []).then((hosts: any[]) => {
        let hostWarn = false;
        let poolWarn = false;
        for (const h of hosts) {
          for (const w of (h.storage_warnings || [])) {
            if (w.mount.includes("/shared") && h.storage_pool_id) poolWarn = true;
            else if (w.level === "warning" || w.level === "critical") hostWarn = true;
          }
        }
        setNavWarnings({ hosts: hostWarn, pools: poolWarn });
      }).catch(() => {});
    };
    check();
    const iv = setInterval(check, 30000);
    return () => clearInterval(iv);
  }, [isAdmin]);

  if (isConsolePage || isPortalPage) {
    return (
      <html lang="en">
        <head><title>{isPortalPage ? "Lab Portal" : "Console"}</title></head>
        {/* Portal page sets its own title via document.title */}
        <body style={{ margin: 0, padding: 0, overflow: "hidden" }}>{children}</body>
      </html>
    );
  }

  const navItems = [
    { label: "Projects", path: "/projects" },
    { label: "Images", path: "/library/images" },
    { label: "Patterns", path: "/library/patterns" },
    { label: "Settings", path: "/settings" },
  ];

  const adminItems = [
    { label: "Users", path: "/admin/users" },
    { label: "Providers", path: "/admin/providers" },
    { label: "Hosts", path: "/admin/hosts" },
    { label: "Storage Pools", path: "/admin/storage-pools" },
    { label: "DNS Providers", path: "/admin/dns-providers" },
  ];

  const masthead = (
    <Masthead>
      <MastheadMain>
        <MastheadBrand>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {isAuthenticated && (
              <PageToggleButton variant="plain" aria-label="Global navigation">
                <BarsIcon style={{ fontSize: "1.5rem" }} />
              </PageToggleButton>
            )}
            <img
              src={isDark ? "/images/troshka-logo-dark-200.png" : "/images/troshka-logo-light-200.png"}
              alt="Troshka"
              style={{ height: "80px", cursor: "pointer" }}
              onClick={() => router.push("/")}
            />
          </div>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <Toolbar>
          <ToolbarContent>
            <ToolbarGroup align={{ default: "alignEnd" }}>
              <ToolbarItem>
                <Button variant="plain" onClick={toggleTheme} aria-label="Toggle theme">
                  {isDark ? <SunIcon /> : <MoonIcon />}
                </Button>
              </ToolbarItem>
              {isAuthenticated && (
                <>
                  <ToolbarItem>
                    <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 14, opacity: 0.85 }}>
                      <UserIcon />
                      {user.display_name || user.email}
                      <select
                        value={user.role}
                        onChange={async (e) => {
                          const newRole = e.target.value;
                          try {
                            const resp = await fetch(`/api/v1/auth/dev-token/${newRole}`);
                            if (!resp.ok) return;
                            const data = await resp.json();
                            localStorage.setItem("troshka-token", data.token);
                            localStorage.setItem("troshka-user", JSON.stringify({
                              id: data.user_id, email: data.email,
                              display_name: data.display_name, role: data.role,
                            }));
                            setUser({ id: data.user_id, email: data.email, display_name: data.display_name, role: data.role });
                          } catch { /* ignore */ }
                        }}
                        style={{
                          fontSize: 11,
                          padding: "1px 6px",
                          borderRadius: 4,
                          border: "none",
                          cursor: "pointer",
                          background: user.role === "admin" ? "rgba(108,99,255,0.2)" : user.role === "operator" ? "rgba(251,191,36,0.2)" : "rgba(148,163,184,0.2)",
                          color: user.role === "admin" ? "#a78bfa" : user.role === "operator" ? "#fbbf24" : "#94a3b8",
                        }}
                      >
                        <option value="admin">admin</option>
                        <option value="operator">operator</option>
                        <option value="user">user</option>
                      </select>
                    </span>
                  </ToolbarItem>
                  <ToolbarItem>
                    <Button variant="plain" onClick={handleLogout} aria-label="Log out">
                      <SignOutAltIcon />
                    </Button>
                  </ToolbarItem>
                </>
              )}
            </ToolbarGroup>
          </ToolbarContent>
        </Toolbar>
      </MastheadContent>
    </Masthead>
  );

  const sidebar = isAuthenticated ? (
    <PageSidebar>
      <PageSidebarBody>
        <Nav>
          <NavList>
            {navItems.map((item) => (
              <NavItem
                key={item.path}
                isActive={pathname === item.path || pathname?.startsWith(item.path + "/")}
                onClick={() => router.push(item.path)}
              >
                {item.label}
              </NavItem>
            ))}
          </NavList>
        </Nav>
        {isAdmin && (
          <Nav aria-label="Admin">
            <NavList title="Admin">
              {adminItems.map((item) => {
                const warn = (item.path === "/admin/hosts" && navWarnings.hosts) ||
                             (item.path === "/admin/storage-pools" && navWarnings.pools);
                return (
                <NavItem
                  key={item.path}
                  isActive={pathname === item.path}
                  onClick={() => router.push(item.path)}
                >
                  {item.label}
                  {warn && <span style={{ color: "#facc15", marginLeft: 6, fontSize: 10 }}>&#9888;</span>}
                </NavItem>
                );
              })}
            </NavList>
          </Nav>
        )}
      </PageSidebarBody>
    </PageSidebar>
  ) : undefined;

  return (
    <html lang="en">
      <head>
        <title>Troshka</title>
        <link rel="icon" href="/images/troshka-logo-32.png" />
      </head>
      <body>
        <Page masthead={masthead} sidebar={sidebar} isManagedSidebar>
          {backendDown && (
            <div style={{
              background: "rgba(220, 38, 38, 0.15)",
              border: "1px solid rgba(220, 38, 38, 0.4)",
              color: "#fca5a5",
              padding: "8px 16px",
              fontSize: 13,
              textAlign: "center",
              fontWeight: 500,
            }}>
              Backend is unreachable — the server may be restarting
            </div>
          )}
          {children}
        </Page>
      </body>
    </html>
  );
}

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
  const isConsolePage = pathname === "/console";
  const isAuthenticated = !!user && !isLoginPage;
  const isAdmin = user?.role === "admin";

  if (isConsolePage) {
    return (
      <html lang="en">
        <head><title>Console</title></head>
        <body style={{ margin: 0, padding: 0, overflow: "hidden" }}>{children}</body>
      </html>
    );
  }

  const navItems = [
    { label: "Projects", path: "/projects" },
    { label: "Library", path: "/library" },
    { label: "Settings", path: "/settings" },
  ];

  const adminItems = [
    { label: "Users", path: "/admin/users" },
    { label: "Providers", path: "/admin/providers" },
    { label: "Hosts", path: "/admin/hosts" },
  ];

  const masthead = (
    <Masthead>
      {isAuthenticated && (
        <MastheadToggle>
          <PageToggleButton variant="plain" aria-label="Global navigation">
            <BarsIcon />
          </PageToggleButton>
        </MastheadToggle>
      )}
      <MastheadMain>
        <MastheadBrand>
          <img
            src={isDark ? "/images/troshka-logo-dark-200.png" : "/images/troshka-logo-light-200.png"}
            alt="Troshka"
            style={{ height: "80px", cursor: "pointer" }}
            onClick={() => router.push("/")}
          />
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
              {adminItems.map((item) => (
                <NavItem
                  key={item.path}
                  isActive={pathname === item.path}
                  onClick={() => router.push(item.path)}
                >
                  {item.label}
                </NavItem>
              ))}
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
          {children}
        </Page>
      </body>
    </html>
  );
}

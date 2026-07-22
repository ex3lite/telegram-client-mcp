import "./style.css";

import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { createApp } from "vue";
import { createWebHashHistory, createRouter } from "vue-router";

import App from "./App.vue";
import { api, ApiError } from "./api";
import LoginView from "./views/LoginView.vue";
import type { AdminIdentity } from "./types";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: false,
      retry: 1
    }
  }
});

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView, meta: { public: true } },
    { path: "/", redirect: "/overview" },
    { path: "/overview", name: "overview", component: () => import("./views/OverviewView.vue") },
    { path: "/runs", name: "runs", component: () => import("./views/RunsView.vue") },
    { path: "/agent", name: "agent", component: () => import("./views/AgentView.vue") },
    { path: "/memory", name: "memory", component: () => import("./views/MemoryView.vue") },
    { path: "/mcp", name: "mcp", component: () => import("./views/McpView.vue") },
    {
      path: "/repositories",
      name: "repositories",
      component: () => import("./views/RepositoriesView.vue")
    },
    { path: "/requests", name: "requests", component: () => import("./views/RequestsView.vue") },
    { path: "/members", name: "members", component: () => import("./views/MembersView.vue") },
    {
      path: "/clarifications",
      name: "clarifications",
      component: () => import("./views/ClarificationsView.vue")
    },
    { path: "/audit", name: "audit", component: () => import("./views/AuditView.vue") },
    { path: "/settings", redirect: "/agent" },
    { path: "/:pathMatch(.*)*", redirect: "/overview" }
  ]
});

router.beforeEach(async (to) => {
  if (to.meta.public) return true;
  try {
    await queryClient.ensureQueryData({
      queryKey: ["auth", "me"],
      queryFn: () => api<AdminIdentity>("/auth/me"),
      staleTime: Number.POSITIVE_INFINITY
    });
    return true;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return { name: "login", query: { next: to.fullPath } };
    }
    return true;
  }
});

createApp(App).use(VueQueryPlugin, { queryClient }).use(router).mount("#app");

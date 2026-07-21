import "@carbon/styles/css/styles.css";
import "./style.css";

import { VueQueryPlugin } from "@tanstack/vue-query";
import { createApp } from "vue";
import { createWebHashHistory, createRouter } from "vue-router";

import App from "./App.vue";
import { api, ApiError } from "./api";
import AuditView from "./views/AuditView.vue";
import ClarificationsView from "./views/ClarificationsView.vue";
import LoginView from "./views/LoginView.vue";
import OverviewView from "./views/OverviewView.vue";
import RepositoriesView from "./views/RepositoriesView.vue";
import RequestsView from "./views/RequestsView.vue";
import SettingsView from "./views/SettingsView.vue";

const router = createRouter({
  history: createWebHashHistory("/admin/"),
  routes: [
    { path: "/login", name: "login", component: LoginView, meta: { public: true } },
    { path: "/", redirect: "/overview" },
    { path: "/overview", name: "overview", component: OverviewView },
    { path: "/requests", name: "requests", component: RequestsView },
    { path: "/clarifications", name: "clarifications", component: ClarificationsView },
    { path: "/repositories", name: "repositories", component: RepositoriesView },
    { path: "/audit", name: "audit", component: AuditView },
    { path: "/settings", name: "settings", component: SettingsView },
    { path: "/:pathMatch(.*)*", redirect: "/overview" }
  ]
});

router.beforeEach(async (to) => {
  if (to.meta.public) return true;
  try {
    await api<{ email: string }>("/auth/me");
    return true;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return { name: "login", query: { next: to.fullPath } };
    }
    return true;
  }
});

createApp(App).use(VueQueryPlugin).use(router).mount("#app");


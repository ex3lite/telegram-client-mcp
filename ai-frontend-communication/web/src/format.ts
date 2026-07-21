import type { Project } from "./types";

export function formatDate(value: string | null | undefined): string {
  if (!value) return "Нет данных";
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(new Date(value));
}

export function shortId(value: string): string {
  return value.slice(0, 8);
}

export function projectName(projects: Project[] | undefined, id: string | null): string {
  if (!id) return "Система";
  return projects?.find((project) => project.id === id)?.name ?? shortId(id);
}


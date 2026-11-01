import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, MemoryRouter } from "react-router-dom";
import { STATIC } from "./api";
import App from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>{STATIC ? <MemoryRouter><App /></MemoryRouter> : <BrowserRouter><App /></BrowserRouter>}</StrictMode>,
);

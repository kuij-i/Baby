/**
 * HTTP Transport layer for BABY API client.
 *
 * Guarantees:
 * - Uses standard fetch API (native in Node.js 18+ and modern browsers).
 * - Enforces request timeout with AbortSignal.
 * - Handles authentication (Bearer token or X-API-Key).
 * - Handles engagement scoping (X-Engagement-ID header).
 * - Safe JSON parsing (handles empty responses, malformed JSON).
 * - NO automatic retries of state-changing operations (prevents duplicate task creation).
 * - Structured error translation matching Baby's fail-closed security contract.
 * - Credential protection: never prints, logs, or echoes credentials in error messages.
 */

import {
  AuthenticationError,
  AuthorizationError,
  BabyClientError,
  ConflictError,
  NetworkError,
  NotFoundError,
  ServerError,
  TimeoutError,
  ValidationError,
} from "./errors.js";

export interface TransportConfig {
  baseUrl: string;
  token?: string;
  apiKey?: string;
  engagementId?: string;
  timeoutMs?: number;
  customHeaders?: Record<string, string>;
  fetchFn?: typeof fetch;
}

export interface RequestOptions {
  method?: string;
  headers?: Record<string, string>;
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
}

export class HttpTransport {
  readonly baseUrl: string;
  private readonly token?: string;
  private readonly apiKey?: string;
  private readonly engagementId?: string;
  private readonly defaultTimeoutMs: number;
  private readonly customHeaders: Record<string, string>;
  private readonly fetchImpl: typeof fetch;

  constructor(config: TransportConfig) {
    if (!config.baseUrl) {
      throw new BabyClientError("baseUrl is required for HttpTransport");
    }
    // Strip trailing slash
    this.baseUrl = config.baseUrl.replace(/\/+$/, "");
    if (config.token) this.token = config.token;
    if (config.apiKey) this.apiKey = config.apiKey;
    if (config.engagementId) this.engagementId = config.engagementId;
    this.defaultTimeoutMs = config.timeoutMs ?? 30_000;
    this.customHeaders = config.customHeaders ?? {};
    this.fetchImpl = config.fetchFn ?? globalThis.fetch;

    if (!this.fetchImpl) {
      throw new BabyClientError("fetch is not available in the current environment");
    }
  }

  /** Build full URL including query parameters. */
  buildUrl(path: string, query?: Record<string, string | number | boolean | null | undefined>): string {
    const cleanPath = path.startsWith("/") ? path : `/${path}`;
    const url = new URL(`${this.baseUrl}${cleanPath}`);

    if (query) {
      for (const [key, val] of Object.entries(query)) {
        if (val !== undefined && val !== null) {
          url.searchParams.append(key, String(val));
        }
      }
    }

    return url.toString();
  }

  /** Execute an HTTP request with timeout, authentication, and error mapping. */
  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method = (options.method ?? "GET").toUpperCase();
    const url = this.buildUrl(path, options.query);
    const timeoutMs = options.timeoutMs ?? this.defaultTimeoutMs;

    const headers: Record<string, string> = {
      Accept: "application/json",
      ...this.customHeaders,
      ...options.headers,
    };

    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }
    if (this.apiKey) {
      headers["X-API-Key"] = this.apiKey;
    }
    if (this.engagementId && !headers["X-Engagement-ID"]) {
      headers["X-Engagement-ID"] = this.engagementId;
    }

    let requestBody: string | undefined;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      try {
        requestBody = JSON.stringify(options.body);
      } catch (err) {
        throw new BabyClientError(`Failed to serialize request body: ${String(err)}`, {
          endpoint: path,
          method,
          code: "SERIALIZATION_ERROR",
        });
      }
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    // If caller provided an external signal, propagate abort
    if (options.signal) {
      options.signal.addEventListener("abort", () => controller.abort(), { once: true });
    }

    let response: Response;
    const fetchInit: RequestInit = {
      method,
      headers,
      signal: controller.signal,
    };
    if (requestBody !== undefined) {
      fetchInit.body = requestBody;
    }

    try {
      response = await this.fetchImpl(url, fetchInit);
    } catch (err: unknown) {
      clearTimeout(timeoutId);
      if (err instanceof Error && err.name === "AbortError") {
        throw new TimeoutError(timeoutMs, { endpoint: path, method });
      }
      const msg = err instanceof Error ? err.message : String(err);
      throw new NetworkError(`Network request failed: ${msg}`, { endpoint: path, method });
    } finally {
      clearTimeout(timeoutId);
    }

    // Process response
    return this.handleResponse<T>(response, path, method);
  }

  private async handleResponse<T>(response: Response, endpoint: string, method: string): Promise<T> {
    const status = response.status;
    let data: unknown;
    const contentType = response.headers.get("content-type") ?? "";

    if (contentType.includes("application/json")) {
      try {
        data = await response.json();
      } catch {
        data = null;
      }
    } else {
      try {
        const text = await response.text();
        data = text ? { detail: text } : null;
      } catch {
        data = null;
      }
    }

    if (response.ok) {
      return data as T;
    }

    // Extract detail and code
    let detailMessage = `Request failed with status ${status}`;
    let errorCode: string | undefined;

    if (data && typeof data === "object") {
      const obj = data as Record<string, unknown>;
      if (typeof obj["detail"] === "string") {
        detailMessage = obj["detail"];
      } else if (Array.isArray(obj["detail"])) {
        detailMessage = JSON.stringify(obj["detail"]);
      }
      if (typeof obj["code"] === "string") {
        errorCode = obj["code"];
      }
    }

    const errorDetails = {
      statusCode: status,
      code: errorCode,
      detail: data,
      endpoint,
      method,
    };

    switch (status) {
      case 401:
        throw new AuthenticationError(detailMessage, errorDetails);
      case 403:
        throw new AuthorizationError(detailMessage, errorDetails);
      case 404:
        throw new NotFoundError(detailMessage, errorDetails);
      case 409:
        throw new ConflictError(detailMessage, errorDetails);
      case 422: {
        const issues = (data && typeof data === "object" && Array.isArray((data as Record<string, unknown>)["detail"]))
          ? (data as Record<string, unknown>)["detail"] as unknown[]
          : [];
        throw new ValidationError(detailMessage, issues, errorDetails);
      }
      default:
        if (status >= 500) {
          throw new ServerError(detailMessage, errorDetails);
        }
        throw new BabyClientError(detailMessage, errorDetails);
    }
  }
}

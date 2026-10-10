/**
 * Structured, type-safe API error hierarchy for BABY client.
 *
 * Distinguishes network failures, timeouts, authentication, authorization,
 * validation errors, not found, state conflicts, and server errors.
 * Preserves fail-closed semantics and ensures credentials/secrets are never leaked.
 */

export interface ApiErrorDetails {
  statusCode?: number | undefined;
  code?: string | undefined;
  detail?: string | unknown;
  endpoint?: string | undefined;
  method?: string | undefined;
}

export class BabyClientError extends Error {
  readonly statusCode?: number;
  readonly code: string;
  readonly endpoint?: string;
  readonly method?: string;

  constructor(message: string, details?: ApiErrorDetails) {
    super(message);
    this.name = "BabyClientError";
    if (details) {
      if (details.statusCode !== undefined) this.statusCode = details.statusCode;
      if (details.endpoint !== undefined) this.endpoint = details.endpoint;
      if (details.method !== undefined) this.method = details.method;
    }
    this.code = details?.code ?? "CLIENT_ERROR";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Network transport failure (DNS, connection refused, offline). */
export class NetworkError extends BabyClientError {
  constructor(message: string, details?: ApiErrorDetails) {
    super(message, { ...details, code: details?.code ?? "NETWORK_ERROR" });
    this.name = "NetworkError";
  }
}

/** Request exceeded configured client timeout. */
export class TimeoutError extends BabyClientError {
  readonly timeoutMs: number;

  constructor(timeoutMs: number, details?: ApiErrorDetails) {
    super(`Request timed out after ${timeoutMs}ms`, {
      ...details,
      code: details?.code ?? "TIMEOUT_ERROR",
    });
    this.name = "TimeoutError";
    this.timeoutMs = timeoutMs;
  }
}

/** Server responded with 401 Unauthorized. */
export class AuthenticationError extends BabyClientError {
  constructor(message: string = "Authentication required or invalid credentials", details?: ApiErrorDetails) {
    super(message, { ...details, statusCode: 401, code: details?.code ?? "UNAUTHORIZED" });
    this.name = "AuthenticationError";
  }
}

/** Server responded with 403 Forbidden. */
export class AuthorizationError extends BabyClientError {
  constructor(message: string = "Permission denied or unauthorized engagement scope", details?: ApiErrorDetails) {
    super(message, { ...details, statusCode: 403, code: details?.code ?? "FORBIDDEN" });
    this.name = "AuthorizationError";
  }
}

/** Server responded with 404 Not Found. */
export class NotFoundError extends BabyClientError {
  constructor(resource: string = "Resource not found", details?: ApiErrorDetails) {
    super(resource, { ...details, statusCode: 404, code: details?.code ?? "NOT_FOUND" });
    this.name = "NotFoundError";
  }
}

/** Server responded with 409 Conflict (e.g. invalid task state machine transition). */
export class ConflictError extends BabyClientError {
  constructor(message: string = "State conflict or invalid state transition", details?: ApiErrorDetails) {
    super(message, { ...details, statusCode: 409, code: details?.code ?? "CONFLICT" });
    this.name = "ConflictError";
  }
}

/** Server responded with 422 Unprocessable Entity (input validation failure). */
export class ValidationError extends BabyClientError {
  readonly validationIssues: unknown[];

  constructor(message: string, issues: unknown[] = [], details?: ApiErrorDetails) {
    super(message, { ...details, statusCode: 422, code: details?.code ?? "VALIDATION_ERROR" });
    this.name = "ValidationError";
    this.validationIssues = issues;
  }
}

/** Server responded with 5xx Internal Server Error or Service Unavailable. */
export class ServerError extends BabyClientError {
  constructor(message: string = "Internal server error", details?: ApiErrorDetails) {
    super(message, { ...details, statusCode: details?.statusCode ?? 500, code: details?.code ?? "SERVER_ERROR" });
    this.name = "ServerError";
  }
}

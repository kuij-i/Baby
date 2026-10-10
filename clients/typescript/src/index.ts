/**
 * @baby/client - Official Typed TypeScript API Client for BABY AI Operating System.
 */

export * from "./types.js";
export {
  BabyClientError,
  NetworkError,
  TimeoutError,
  AuthenticationError,
  AuthorizationError,
  NotFoundError,
  ConflictError,
  ValidationError,
  ValidationError as ClientValidationError,
  ServerError,
  type ApiErrorDetails,
} from "./errors.js";
export * from "./transport.js";
export * from "./apis.js";
export * from "./client.js";

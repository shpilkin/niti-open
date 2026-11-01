"""Connector layer: provider adapters. MVP ships IMAP only (polling-based)."""

from email_reader.connectors.imap import FetchedMessage, fetch_new

__all__ = ["FetchedMessage", "fetch_new"]

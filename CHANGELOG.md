# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Security
- [Security] Upgrade cryptography from 42.0.8 to >=44.0.1 to fix CVE-2024-12797 (OpenSSL X.509 certificate verification bypass) (PR #2)

## [2026-02-27]

### Security
- [Security] Upgrade jinja2 from 3.1.4 to 3.1.6 to fix CVE-2024-56201 and CVE-2024-56326 (sandbox escape via `|attr` filter chains and `__init_subclass__`) (PR #1)

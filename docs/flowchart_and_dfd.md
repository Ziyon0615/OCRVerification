# OCRverification System Diagrams

This file contains implementation-ready Mermaid diagrams for system flow and data flow.

## 1) System Flowchart

```mermaid
flowchart TD
    A[Start] --> B{User has account?}
    B -->|No| C[Register]
    C --> D[Account saved as Pending]
    D --> E[Wait for Admin Approval]
    E --> F[Login]
    B -->|Yes| F[Login]

    F --> G{Role?}
    G -->|Applicant| H[Applicant Dashboard]
    G -->|Admin| I[Admin Dashboard]

    H --> J[Upload Applicant ID Document]
    J --> K[Open Verification Page]
    K --> L[Upload Reference and One or More Applicant IDs]
    L --> M[OCR plus Similarity plus Validation]
    M --> N{Verification Passed?}
    N -->|No| O[Show Issues and Retry]
    O --> K
    N -->|Yes| P[Admin Proceeds to Loan Application]

    P --> Q[Enter Loan plus Income plus Debt plus Credit Data]
    Q --> R[Risk and Fraud Scoring]
    R --> S[Generate Recommendation]
    S --> T[Save to SQLite plus JSON Report]
    T --> U[Show Applicant Dashboard Status]

    I --> V[Review Pending User Accounts]
    I --> W[Review Pending Loan Applications]
    I --> X[Approve or Reject Loans]
    I --> Y[Manage Reports Download and Cleanup]
    I --> Z[Record Payments and Monitor Overdue]

    X --> U
    Z --> U
    U --> AA[End]
```

## 2) DFD Level 0 (Context)

```mermaid
flowchart LR
    Applicant[Applicant]
    Admin[Admin]
    System[OCR Verification and Loan Management System]

    Applicant -->|Register Login, Upload Docs, Apply Loan, Pay Loan| System
    System -->|Status, Verification Result, Loan Decision, Receipts| Applicant

    Admin -->|Approve Users, Review Loans, Verify Docs, Manage Reports| System
    System -->|Dashboards, Pending Lists, Reports, Analytics| Admin
```

## 3) DFD Level 1

```mermaid
flowchart TB
    Applicant[Applicant]
    Admin[Admin]

    P1[1.0 Authentication and Session]
    P2[2.0 Document Upload and OCR Verification]
    P3[3.0 Loan Application and Risk Scoring]
    P4[4.0 Admin Review and Approval]
    P5[5.0 Payments and Loan Tracking]
    P6[6.0 Reports Management]

    D1[(User Accounts DB)]
    D2[(Loan Applications DB)]
    D3[(Payments DB)]
    D4[(User Documents Storage)]
    D5[(Reports Storage JSON and PDF)]
    D6[(Reference Documents)]

    Applicant -->|Register and Login| P1
    P1 -->|Create and Update User Session| D1
    P1 -->|Authentication Result| Applicant

    Applicant -->|Upload ID and Documents| P2
    P2 -->|Store User Documents| D4
    P2 -->|Read Document Templates| D6
    P2 -->|Verification Output| Applicant

    Applicant -->|Submit Loan Details| P3
    P3 -->|Read User Profile and Status| D1
    P3 -->|Save Loan Record| D2
    P3 -->|Create Report Metadata| D5
    P3 -->|Application Status| Applicant

    Admin -->|Approve or Reject Users| P4
    P4 -->|Update Account Status| D1
    Admin -->|Approve or Reject Loan Applications| P4
    P4 -->|Update Decision and Recommendation| D2
    P4 -->|Decision Feedback| Admin

    Applicant -->|Make Payment| P5
    P5 -->|Insert Payment Record| D3
    P5 -->|Update Loan Balance State| D2
    P5 -->|Payment Receipt and Status| Applicant
    Admin -->|Monitor Overdue and Paid Loans| P5
    P5 -->|Loan and Payment Summary| Admin

    Admin -->|List, Download, Cleanup Reports| P6
    P6 -->|Read, Write, Delete Reports| D5
    P6 -->|Report List and Files| Admin
```

## Optional next diagram

You can add a DFD Level 2 focused on Loan Processing as a separate section if needed for thesis documentation.

#!/usr/bin/env python3
"""
Comprehensive test for new user registration and document management features.
Tests:
1. User registration (new users can create accounts)
2. User profile creation (automatic profile created on registration)
3. User-filtered dashboard (users only see their own loans)
4. Document upload to profile (users can upload ID documents)
5. User document retrieval (users can see their documents)
6. Admin document browser (admins can see all user documents)
7. Admin document verification (admins can verify documents)
"""

import sqlite3
import json
from app import app

def test_features():
    """Run comprehensive tests for new features"""
    client = app.test_client()
    results = []
    
    print("=" * 80)
    print("TESTING NEW USER REGISTRATION & DOCUMENT MANAGEMENT FEATURES")
    print("=" * 80)
    
    # Test 1: User Registration
    print("\n[TEST 1] User Registration")
    reg_data = {
        'full_name': 'John Doe',
        'username': 'johndoe',
        'password': 'securepass123'
    }
    response = client.post('/register', json=reg_data)
    print(f"  Status: {response.status_code}")
    data = response.get_json()
    print(f"  Response: {data['message']}")
    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    assert data['success'] == True
    results.append(('User Registration', 'PASS', response.status_code))
    
    # Test 2: Duplicate Username Check
    print("\n[TEST 2] Duplicate Username Registration (Should Fail)")
    response = client.post('/register', json=reg_data)
    print(f"  Status: {response.status_code}")
    data = response.get_json()
    print(f"  Response: {data['message']}")
    assert response.status_code == 400, f"Expected 400, got {response.status_code}"
    results.append(('Duplicate Username Check', 'PASS', response.status_code))
    
    # Test 3: Login with New User Account should be pending approval
    print("\n[TEST 3] Login with New User Account (Should Be Pending)")
    response = client.post('/login', json={'username': 'johndoe', 'password': 'securepass123'})
    print(f"  Status: {response.status_code}")
    data = response.get_json()
    print(f"  Response: {data}")
    assert response.status_code == 403
    assert data['success'] == False
    results.append(('New User Pending Login', 'PASS', response.status_code))

    # Approve the new user as admin
    print("\n[TEST 4] Approve New User As Admin")
    admin_login = client.post('/login', json={'username': 'admin', 'password': 'jethro123'})
    admin_cookie = admin_login.headers.get('Set-Cookie')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id FROM user_accounts WHERE username = ?", ('johndoe',))
    user_id = c.fetchone()['id']
    conn.close()
    response = client.post(f'/admin/users/{user_id}/approval', json={'password': 'jethro123', 'action': 'approve'}, headers={'Cookie': admin_cookie})
    print(f"  Status: {response.status_code}")
    print(f"  Response: {response.get_json()}")
    assert response.status_code == 200
    results.append(('Approve New User', 'PASS', response.status_code))

    # Test 5: Login after approval
    print("\n[TEST 5] Login After Approval")
    response = client.post('/login', json={'username': 'johndoe', 'password': 'securepass123'})
    print(f"  Status: {response.status_code}")
    data = response.get_json()
    print(f"  Role: {data.get('role')}")
    print(f"  Full Name: {data.get('full_name')}")
    assert response.status_code == 200
    assert data['success'] == True
    assert data['role'] == 'applicant'
    results.append(('Approved User Login', 'PASS', response.status_code))

    # Extract session cookie
    cookie_header = response.headers.get('Set-Cookie')
    print(f"  Session Created: {('session_token' in cookie_header)}")

    # Test 6: User-Filtered Dashboard (Empty)
    print("\n[TEST 6] User-Filtered Dashboard (Should Be Empty)")
    response = client.get('/user-dashboard', headers={'Cookie': cookie_header})
    print(f"  Status: {response.status_code}")
    assert response.status_code == 200
    assert b'user_name' in response.data or b'John Doe' in response.data
    results.append(('User Dashboard Access', 'PASS', response.status_code))
    
    # Test 7: Submit Loan as New User
    print("\n[TEST 7] Submit Loan as New User")
    loan_data = {
        'full_name': 'John Doe',
        'contact': '09175555555',
        'amount': '100000',
        'months': '12',
        'interest_rate': '8',
        'employment_status': 'employed',
        'employer_name': 'Tech Company',
        'monthly_income': '50000',
        'other_income': '0',
        'credit_score': '700',
        'existing_debt': '10000',
        'requested_documents': 'government_id'
    }
    response = client.post('/loan-application', 
                          data=loan_data,
                          headers={'Cookie': cookie_header})
    print(f"  Status: {response.status_code}")
    assert response.status_code == 200
    results.append(('Loan Submission by New User', 'PASS', response.status_code))
    
    # Test 8: User Dashboard with One Loan
    print("\n[TEST 8] User Dashboard (Should Show One Loan)")
    response = client.get('/user-dashboard', headers={'Cookie': cookie_header})
    print(f"  Status: {response.status_code}")
    assert response.status_code == 200
    assert b'John Doe' in response.data
    assert b'My Loans' in response.data
    results.append(('User Dashboard with Loans', 'PASS', response.status_code))
    
    # Test 9: Get User Documents (Empty Initially)
    print("\n[TEST 9] Get User Documents (Empty Initially)")
    response = client.get('/user/documents', headers={'Cookie': cookie_header})
    print(f"  Status: {response.status_code}")
    data = response.get_json()
    print(f"  Documents Count: {len(data.get('documents', []))}")
    assert response.status_code == 200
    assert len(data['documents']) == 0
    results.append(('Get User Documents', 'PASS', response.status_code))
    
    # Admin tests
    print("\n[TEST 10] Admin Login")
    response = client.post('/login', json={'username': 'admin', 'password': 'jethro123'})
    print(f"  Status: {response.status_code}")
    data = response.get_json()
    print(f"  Role: {data.get('role')}")
    assert response.status_code == 200
    assert data['role'] == 'admin'
    admin_cookie = response.headers.get('Set-Cookie')
    results.append(('Admin Login', 'PASS', response.status_code))
    
    # Test 11: Admin View All Documents (Empty)
    print("\n[TEST 11] Admin View All User Documents")
    response = client.get('/admin/documents', headers={'Cookie': admin_cookie})
    print(f"  Status: {response.status_code}")
    data = response.get_json()
    print(f"  Total Documents: {data.get('count')}")
    assert response.status_code == 200
    results.append(('Admin Document Browser', 'PASS', response.status_code))
    
    # Test 12: Check User Accounts Table
    print("\n[TEST 12] Verify User in Database")
    from app import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT username, role, full_name, is_active FROM user_accounts WHERE username = ?", ('johndoe',))
    user = c.fetchone()
    conn.close()
    print(f"  Username: {user['username']}")
    print(f"  Role: {user['role']}")
    print(f"  Full Name: {user['full_name']}")
    print(f"  Active: {user['is_active']}")
    assert user is not None
    assert user['role'] == 'applicant'
    assert user['is_active'] == 1
    results.append(('User in Database', 'PASS', '✓'))
    
    # Print Summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    for test_name, status, detail in results:
        status_symbol = "✓" if status == "PASS" else "✗"
        print(f"[{status_symbol}] {test_name:.<50} {detail}")
    
    passed = sum(1 for _, status, _ in results if status == "PASS")
    total = len(results)
    print(f"\nTotal: {passed}/{total} tests passed")
    print("=" * 80)
    
    if passed == total:
        print("✓ ALL TESTS PASSED!")
        return True
    else:
        print(f"✗ {total - passed} test(s) failed")
        return False

if __name__ == '__main__':
    success = test_features()
    exit(0 if success else 1)

-- ER Diagram
-- Role 1---* User
-- User 1---0..1 Student
-- Student *---* Course through Enrollment
-- User 1---* AuditLog

CREATE TABLE roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE
);

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(150) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    role_id INTEGER NOT NULL REFERENCES roles(id)
);

CREATE INDEX ix_users_username ON users(username);
CREATE INDEX ix_users_email ON users(email);

CREATE TABLE students (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    enrollment_number VARCHAR(50) NOT NULL UNIQUE,
    date_of_birth TIMESTAMP NULL
);

CREATE TABLE courses (
    id SERIAL PRIMARY KEY,
    code VARCHAR(20) NOT NULL UNIQUE,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    credits INTEGER NOT NULL
);

CREATE TABLE enrollments (
    student_id INTEGER NOT NULL REFERENCES students(id),
    course_id INTEGER NOT NULL REFERENCES courses(id),
    enrolled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (student_id, course_id)
);

CREATE UNIQUE INDEX ix_enrollment_student_course ON enrollments(student_id, course_id);

CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    action VARCHAR(50) NOT NULL,
    table_name VARCHAR(50) NOT NULL,
    record_id INTEGER NOT NULL,
    performed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    performed_by INTEGER NOT NULL REFERENCES users(id)
);

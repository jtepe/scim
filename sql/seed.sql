-- Destructive local reset. Never changes the service provider.
PRAGMA foreign_keys = ON;
BEGIN;
DELETE FROM journal;
DELETE FROM runs;
DELETE FROM memberships;
DELETE FROM users;
DELETE FROM groups;
DELETE FROM companies;
DELETE FROM settings;
INSERT INTO settings(id) VALUES(1);
INSERT INTO companies VALUES ('company-001', 'Roshar Industries');
INSERT INTO users(id,company_id,username,given_name,family_name,email,active) VALUES
 ('user-kaladin','company-001','kaladin@roshar.example','Kaladin','Stormblessed','kaladin@roshar.example',1),
 ('user-shallan','company-001','shallan@roshar.example','Shallan','Davar','shallan@roshar.example',1),
 ('user-dalinar','company-001','dalinar@roshar.example','Dalinar','Kholin','dalinar@roshar.example',1),
 ('user-jasnah','company-001','jasnah@roshar.example','Jasnah','Kholin','jasnah@roshar.example',1),
 ('user-adolin','company-001','adolin@roshar.example','Adolin','Kholin','adolin@roshar.example',1),
 ('user-navani','company-001','navani@roshar.example','Navani','Kholin','navani@roshar.example',1),
 ('user-szeth','company-001','szeth@roshar.example','Szeth','Son-son-Vallano','szeth@roshar.example',0);
INSERT INTO groups(id,company_id,display_name) VALUES
 ('group-employees','company-001','All Employees'),
 ('group-engineering','company-001','Engineering'),
 ('group-hr','company-001','Human Resources'),
 ('group-finance','company-001','Finance'),
 ('group-it','company-001','IT Administrators');
INSERT INTO memberships VALUES
 ('user-kaladin','group-employees'),('user-shallan','group-employees'),
 ('user-dalinar','group-employees'),('user-jasnah','group-employees'),
 ('user-adolin','group-employees'),('user-navani','group-employees'),
 ('user-kaladin','group-engineering'),('user-navani','group-engineering'),
 ('user-shallan','group-hr'),('user-dalinar','group-finance'),
 ('user-jasnah','group-it');
COMMIT;

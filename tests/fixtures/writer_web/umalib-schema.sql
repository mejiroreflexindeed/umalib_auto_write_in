-- 合成测试 Schema（TASK-009，仅 DDL，不含任何真实数据）。
-- 声明类型逐字复刻自已确认契约，用于生成已确认 Schema 指纹
-- d7b1966992e75819e37d41ae9c091ec21c2cdf9c5b6727af78f95398d0179aa8。
CREATE TABLE [article](
  [id] integer PRIMARY KEY AUTOINCREMENT, 
  [name] varchar(50), 
  [note] varchar(100), 
  [content] text, 
  [author] varchar(50), 
  [translator] varchar(50), 
  [uploadTime] INTEGER, 
  [source] varchar(128));

CREATE TABLE creator (id INTEGER PRIMARY KEY, names TEXT);

CREATE TABLE dict (id INTEGER PRIMARY KEY AUTOINCREMENT, class varchar (10), desc TEXT NOT NULL, key varchar (50), refId INTEGER, related varchar (50), relatedId INTEGER);

CREATE TABLE rec (id INTEGER PRIMARY KEY AUTOINCREMENT, refId INTEGER NOT NULL, title varchar (50) NOT NULL, others varchar (100) DEFAULT (''), type INTEGER DEFAULT (0), r INTEGER DEFAULT (0), name varchar (50) NOT NULL, reason varchar (50) NOT NULL);

CREATE TABLE tag (id integer PRIMARY KEY AUTOINCREMENT, name varchar (50), type integer, cover TEXT, description VARCHAR (512));

CREATE TABLE tagged (id integer primary key autoincrement, artId integer not null, tagId integer not null, foreign key (artId) references article (id), foreign key (tagId) references tag (id) );

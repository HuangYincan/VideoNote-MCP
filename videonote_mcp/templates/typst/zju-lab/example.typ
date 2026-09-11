#import "imports.typ": *
#import "template.typ": project, indent

#show: project.with(
  course: "计算机网络",
  lab_name: "TCP/IP 实验报告",
  stu_name: "张三",
  stu_num: "3200000000",
  major: "计算机科学与技术",
  department: "计算机学院",
  date: (2026, 8, 4),
  show_content_figure: true,
  watermark: "ZJU",
)

= 实验目的

通过本次实验，理解 TCP/IP 协议栈的层次结构与封装过程，掌握使用抓包工具分析网络报文的方法。

== 实验原理

TCP/IP 协议栈自顶向下分为应用层、传输层、网络层与链路层。数据发送时逐层封装：

$ "HTTP" -> "TCP" -> "IP" -> "以太网帧" $

= 实验内容

== 抓包分析

使用 `tcpdump` 抓取一次 HTTP 请求，可见五元组与各层头部字段：

```bash
tcpdump -i eth0 -n 'tcp port 80'
```

表 @tcp 列出了抓包结果中的关键字段。

= 结果与分析

#figure(
  table(
    columns: 3,
    [字段], [值], [说明],
    [源端口], [54321], [客户端临时端口],
    [目的端口], [80], [HTTP 服务端口],
    [序号], [1], [TCP 序列号],
  ),
  caption: [TCP 报文关键字段],
) <tcp>

#abstract[这是一个展示用的简单示例，用于演示模板的封面、目录、公式、代码块、表格与参考文献等能力。]

#bibliography("works.bib", title: [参考文献])
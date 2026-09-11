#import "imports.typ": *

#let Songti = ("Times New Roman", "Songti SC", "Songti TC")
#let Heiti = ("Times New Roman", "Heiti SC", "Heiti TC", "SimHei")

#let indent() = {
  box(width: 2em)
}

#let info_key(body) = {
  rect(width: 100%, inset: 2pt, stroke: none, text(font: Songti, size: 16pt, body))
}

#let info_value(body) = {
  rect(
    width: 100%,
    inset: 2pt,
    stroke: (bottom: 1pt + black),
    text(font: Songti, size: 16pt, bottom-edge: "descender")[ #body ],
  )
}

#let project(
  course: "COURSE",
  lab_name: "LAB NAME",
  stu_name: "NAME",
  stu_num: "1234567",
  major: "MAJOR",
  department: "DEPARTMENT",
  date: (2077, 1, 1),
  show_content_figure: false,
  watermark: "",
  body,
) = {
  set page("a4")
  // 封面
  align(center)[
    #v(-2em)
    #image("./img/ZJU-name.png", width: 60%)
    #set text(
      size: 26pt,
      font: Songti,
      weight: "bold",
    )

    // 课程名
    #text(size: 25pt, font: Songti)[
      _#course _课程实验报告
    ]
    #v(1em)

    // 报告名
    #text(size: 22pt, font: Songti)[
      _#lab_name _
    ]
    #image("./img/ZJU-logo.png", width: 40%)
    #v(0.5em)

    // 个人信息
    #grid(
      columns: (70pt, 160pt),
      rows: (40pt, 40pt),
      gutter: 3pt,
      info_key("学院"),
      info_value(department),
      info_key("专业"),
      info_value(major),
      info_key("姓名"),
      info_value(stu_name),
      info_key("学号"),
      info_value(stu_num),
    )
    #v(1pt)

    // 日期
    #text(font: Songti, size: 14pt)[
      #date.at(0) 年 #date.at(1) 月 #date.at(2) 日
    ]
  ]
  pagebreak()

  // 目录
  show outline.entry.where(level: 1): it => {
    v(12pt, weak: true)
    strong(it)
  }
  show outline.entry: it => {
    set text(
      font: Songti,
      size: 12pt,
    )
    it
  }
  outline(
    title: text(font: Songti, size: 16pt)[目录],
    indent: auto,
  )
  if show_content_figure {
    text(font: Songti, size: 10pt)[
      #i-figured.outline(title: [图表])
    ]
  }
  pagebreak()

  // 页眉页脚设置
  show: chic.with(
    chic-header(
      left-side: smallcaps(
        text(size: 10pt, font: Songti)[
          #course -- #lab_name
        ],
      ),
      right-side: text(size: 10pt, font: Songti)[
        #chic-heading-name(dir: "prev")
      ],
      side-width: (60%, 0%, 35%),
    ),
    chic-footer(
      center-side: text(size: 11pt, font: Songti)[
        #chic-page-number()
      ],
    ),
    chic-separator(on: "header", chic-styled-separator("bold-center")),
    chic-separator(on: "footer", stroke(dash: "loosely-dashed", paint: gray)),
    chic-offset(40%),
    chic-height(2cm),
  )

  // 正文设置
  show: thmbox-init()
  set heading(numbering: "1.1")
  set figure(supplement: [图])
  show heading: i-figured.reset-counters.with(level: 2)
  show figure: i-figured.show-figure.with(level: 2)
  show math.equation: i-figured.show-equation
  set text(
    font: Songti,
    size: 12pt,
  )
  set par(    // 段落设置
    justify: false,
    leading: 1.04em,
    first-line-indent: 2em,
  )
  show heading: it => box(width: 100%)[ // 标题设置
    #v(0.45em)
    #set text(font: Songti)
    #if it.numbering != none {
      counter(heading).display()
    }
    #h(0.75em)
    #it.body
    #v(5pt)
  ]
  show link: it => {          // 链接
    set text(fill: blue.darken(20%))
    it
  }
  show: gentle-clues.with(    // gentle块
    headless: false, // never show any headers
    breakable: true, // default breaking behavior
    header-inset: 0.4em, // default header-inset
    content-inset: 1em, // default content-inset
    stroke-width: 2pt, // default left stroke-width
    border-radius: 2pt, // default border-radius
    border-width: 0.5pt, // default boarder-width
  )
  show: checklist.with(fill: luma(95%), stroke: blue, radius: .2em)   // 复选框

  // 代码段设置
  show: codly-init.with()
  codly(languages: codly-languages)

  // 水印
  set page(background: rotate(-60deg,
  text(100pt, fill: rgb("#faf2f1"))[
      #strong()[#watermark]
    ]
  ))

  body
}
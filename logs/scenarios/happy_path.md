# Сценарій: happy_path

- **AGENT → manager:** Good afternoon, I am an AI assistant booking on behalf of my client. Would a table for two at 17:00 today be available?
- **MANAGER:** Good afternoon! Yes, 5pm for two is fine. What name shall I put it under?
- **AGENT → manager:** Let me just check that and come back to you in a moment.
- **AGENT → operator:** 🤖 [agent] Потрібна ваша відповідь: What name should I use for the booking?
(Напишіть відповідь сюди, у Saved Messages.)
- **OPERATOR:** Sasha
- **AGENT → manager:** Thank you, I've noted the name Sasha. Could you please let me know the restaurant name?
- **MANAGER:** Lovely, that's booked for Sasha at The Golden Lion, 5pm today, table for two.
- **AGENT → manager:** Wonderful, thank you ever so much. Just to confirm the booking: a table for two (2 people) at 17:00 (5 pm) today, Sunday, 22 November 2026, at The Golden Lion under the name Sasha. We look forward to it. Many thanks again!
- **AGENT → operator:** 🤖 [agent] ✅ Бронь підтверджено: Wonderful, thank you ever so much. Just to confirm the booking: a table for two (2 people) at 17:00 (5 pm) today, Sunday, 22 November 2026, at The Golden Lion under the name Sasha. We look forward to it. Many thanks again!
- **MANAGER:** See you later!
- **AGENT → manager:** Thank you, see you later. Have a wonderful day!

Фінальний статус: `closed`

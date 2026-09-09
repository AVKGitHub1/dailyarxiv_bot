make a docker for this. It should retain the same functionality with following additions:
- make a GUI that is published as a webpage on a port (select an uncommon port for this)
- the GUI should:
- have textboxes showing the authors and keywords. It should have boxes so that a user can suggest additions for the author and keyword
- the gui should also have an admin account that needs a password (displayed when the container is first launched). This admin account should have same functionality as public but with the added ability to accept the author and keyword additions
- the GUI should also display the list of papers that were sent in the last message. Each entry should have a link to the arxiv and upon clicking the title should display the abstract of that paper.
- the admin account should have the ability to regenerate the list and send it again as a slack message.

Ask questions before proceeding